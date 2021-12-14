from __future__ import absolute_import

import logging
import random
import re
import time
from functools import partial
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    SupportsFloat,
    SupportsInt,
    Tuple,
)

import simplejson as json
from confluent_kafka import KafkaError
from confluent_kafka import Message as KafkaMessage
from confluent_kafka import Producer

from snuba import environment, settings
from snuba.redis import redis_client
from snuba.utils.metrics.wrapper import MetricsWrapper
from snuba.utils.streams.configuration_builder import (
    build_default_kafka_producer_configuration,
)
from snuba.utils.streams.topics import Topic

metrics = MetricsWrapper(environment.metrics, "snuba.state")
logger = logging.getLogger("snuba.state")

kfk = None
rds = redis_client

ratelimit_prefix = "snuba-ratelimit:"
query_lock_prefix = "snuba-query-lock:"
config_hash = "snuba-config"
config_history_hash = "snuba-config-history"
config_changes_list = "snuba-config-changes"
config_changes_list_limit = 25
queries_list = "snuba-queries"

# Rate Limiting and Deduplication

# Window for concurrent query counting
max_query_duration_s = 60
# Window for determining query rate
rate_lookback_s = 60


def get_concurrent(bucket: str) -> Any:
    now = time.time()
    bucket = "{}{}".format(ratelimit_prefix, bucket)
    return rds.zcount(bucket, "({:f}".format(now), "+inf")


def get_rates(bucket: str, rollup: int = 60) -> Sequence[Any]:
    now = int(time.time())
    bucket = "{}{}".format(ratelimit_prefix, bucket)
    pipe = rds.pipeline(transaction=False)
    rate_history_s = get_config("rate_history_sec", 3600)
    assert rate_history_s is not None
    for i in reversed(range(now - rollup, now - rate_history_s, -rollup)):
        pipe.zcount(bucket, i, "({:f}".format(i + rollup))
    return [c / float(rollup) for c in pipe.execute()]


# Runtime Configuration


class memoize:
    """
    Simple expiring memoizer for functions with no args.
    """

    def __init__(self, timeout: int = 1) -> None:
        self.timeout = timeout
        self.saved = None
        self.at = 0.0

    def __call__(self, func: Callable[[], Any]) -> Callable[[], Any]:
        def wrapper() -> Any:
            now = time.time()
            if now > self.at + self.timeout or self.saved is None:
                self.saved, self.at = func(), now
            return self.saved

        return wrapper


def numeric(value: Any) -> Any:
    try:
        assert isinstance(value, (str, SupportsInt))
        return int(value)
    except (ValueError, AssertionError):
        try:
            assert isinstance(value, (str, SupportsFloat))
            return float(value)
        except (ValueError, AssertionError):
            return value


ABTEST_RE = re.compile("(?:(-?\d+\.?\d*)(?:\:(\d+))?\/?)")


def abtest(value: Optional[Any]) -> Optional[Any]:
    """
    Recognizes a value that consists of a '/'-separated sequence of
    value:weight tuples. Value is numeric. Weight is an optional integer and
    defaults to 1. Returns a weighted random value from the set of values.
    eg.
    1000/2000 => returns 1000 or 2000 with equal weight
    1000:1/2000:1 => returns 1000 or 2000 with equal weight
    1000:2/2000:1 => returns 1000 twice as often as 2000
    """
    if isinstance(value, str) and ABTEST_RE.match(value):
        values = ABTEST_RE.findall(value)
        total_weight = sum(int(weight or 1) for (_, weight) in values)
        r = random.randint(1, total_weight)
        i = 0
        for (v, weight) in values:
            i += int(weight or 1)
            if i >= r:
                return numeric(v)

    return value


def set_config(key: str, value: Optional[Any], user: Optional[str] = None) -> None:
    if value is not None:
        value = "{}".format(value).encode("utf-8")

    try:
        original_value = rds.hget(config_hash, key)
        if value == original_value:
            return

        change_record = (time.time(), user, original_value, value)
        if value is None:
            rds.hdel(config_hash, key)
            rds.hdel(config_history_hash, key)
        else:
            rds.hset(config_hash, key, value)
            rds.hset(config_history_hash, key, json.dumps(change_record))
        rds.lpush(config_changes_list, json.dumps((key, change_record)))
        rds.ltrim(config_changes_list, 0, config_changes_list_limit)
    except Exception as ex:
        logger.exception(ex)


def set_configs(
    values: Mapping[str, Optional[Any]], user: Optional[str] = None
) -> None:
    for k, v in values.items():
        set_config(k, v, user=user)


def get_config(key: str, default: Optional[Any] = None) -> Optional[Any]:
    return get_all_configs().get(key, default)


def get_configs(
    key_defaults: Iterable[Tuple[str, Optional[Any]]]
) -> Sequence[Optional[Any]]:
    all_confs = get_all_configs()
    return [all_confs.get(k, d) for k, d in key_defaults]


def get_all_configs() -> Mapping[str, Optional[Any]]:
    return {k: abtest(v) for k, v in get_raw_configs().items()}


@memoize(settings.CONFIG_MEMOIZE_TIMEOUT)
def get_raw_configs() -> Mapping[str, Optional[Any]]:
    try:
        all_configs = rds.hgetall(config_hash)
        return {
            k.decode("utf-8"): numeric(v.decode("utf-8"))
            for k, v in all_configs.items()
            if v is not None
        }
    except Exception as ex:
        logger.exception(ex)
        return {}


def delete_config(key: str, user: Optional[Any] = None) -> None:
    return set_config(key, None, user=user)


def get_uncached_config(key: str) -> Optional[Any]:
    value = rds.hget(config_hash, key.encode("utf-8"))
    if value is not None:
        return numeric(value.decode("utf-8"))
    return None


def get_config_changes_legacy() -> Sequence[Any]:
    return [json.loads(change) for change in rds.lrange(config_changes_list, 0, -1)]


def get_config_changes() -> Sequence[Tuple[str, float, Optional[str], Any, Any]]:
    """
    Like get_config_changes_legacy() but ensures that values are cast to their correct type
    """
    changes = get_config_changes_legacy()

    return [
        (key, ts, user, numeric(before), numeric(after))
        for [key, [ts, user, before, after]] in changes
    ]


# Query Recording


def safe_dumps_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {**value}
    raise TypeError(
        f"Cannot convert object of type {type(value).__name__} to JSON-safe type"
    )


safe_dumps = partial(json.dumps, for_json=True, default=safe_dumps_default)


def _record_query_delivery_callback(
    error: Optional[KafkaError], message: KafkaMessage
) -> None:
    metrics.increment(
        "record_query.delivery_callback",
        tags={"status": "success" if error is None else "failure"},
    )

    if error is not None:
        logger.warning("Could not record query due to error: %r", error)


def record_query(query_metadata: Mapping[str, Any]) -> None:
    global kfk
    max_redis_queries = 200
    try:
        data = safe_dumps(query_metadata)
        rds.pipeline(transaction=False).lpush(queries_list, data).ltrim(  # type: ignore
            queries_list, 0, max_redis_queries - 1
        ).execute()

        if kfk is None:
            kfk = Producer(build_default_kafka_producer_configuration())

        kfk.poll(0)  # trigger queued delivery callbacks
        kfk.produce(
            settings.KAFKA_TOPIC_MAP.get(Topic.QUERYLOG.value, Topic.QUERYLOG.value),
            data.encode("utf-8"),
            on_delivery=_record_query_delivery_callback,
        )
    except Exception as ex:
        logger.exception("Could not record query due to error: %r", ex)


def get_queries() -> Sequence[Mapping[str, Optional[Any]]]:
    try:
        queries = []
        for q in rds.lrange(queries_list, 0, -1):
            try:
                queries.append(json.loads(q))
            except BaseException:
                pass
    except Exception as ex:
        logger.exception(ex)

    return queries


def is_project_in_rollout_list(rollout_key: str, project_id: int) -> bool:
    """
    Helper function for doing selective rollouts by project ids. The config
    value is assumed to be a string of the form `[project,project,...]`.

    Returns `True` if `project_id` is present in the config.
    """
    project_rollout_setting = get_config(rollout_key, "")
    assert isinstance(project_rollout_setting, str)
    if project_rollout_setting:
        # The expected format is [project,project,...]
        project_rollout_setting = project_rollout_setting[1:-1]
        if project_rollout_setting:
            for p in project_rollout_setting.split(","):
                if int(p.strip()) == project_id:
                    return True
    return False


def is_project_in_rollout_group(rollout_key: str, project_id: int) -> bool:
    """
    Helper function for doing consistent incremental rollouts by project ids.
    The config value is assumed to be an integer between 0 and 100.

    Returns `True` if `project_id` falls within the rollout group.
    """
    project_rollout_percentage = get_config(rollout_key, 0)
    assert isinstance(project_rollout_percentage, (int, float))
    if project_rollout_percentage:
        return project_id % 100 < project_rollout_percentage
    return False
