import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from snuba import optimize, settings, util
from snuba.clickhouse.native import ClickhousePool, ClickhouseResult
from snuba.clusters.cluster import ClickhouseClientSettings
from snuba.datasets.storage import WritableTableStorage
from snuba.datasets.storages.factory import get_writable_storage
from snuba.datasets.storages.storage_key import StorageKey
from snuba.optimize import run_optimize_cron_job
from snuba.optimize_tracker import NoOptimizedStateException, OptimizedPartitionTracker
from snuba.processor import InsertBatch
from snuba.redis import RedisClientKey, get_redis_client
from tests.helpers import write_processed_messages

redis_client = get_redis_client(RedisClientKey.REPLACEMENTS_STORE)


@pytest.mark.parametrize(
    "tracker",
    [
        pytest.param(
            OptimizedPartitionTracker(
                redis_client=redis_client,
                host="some-hostname.domain.com",
                port=9000,
                database="some-database",
                table="some-table",
                expire_time=(datetime.now() + timedelta(minutes=3)),
            ),
            id="redis",
        ),
    ],
)
def test_optimized_partition_tracker(tracker: OptimizedPartitionTracker) -> None:
    assert len(tracker.get_all_partitions()) == 0
    assert len(tracker.get_scheduled_partitions()) == 0
    with pytest.raises(NoOptimizedStateException):
        tracker.get_partitions_to_optimize()

    tracker.update_all_partitions(["Partition 1", "Partition 2"])
    tracker.update_scheduled_partitions("Partition 1")
    assert tracker.get_scheduled_partitions() == {"Partition 1"}
    assert tracker.get_partitions_to_optimize() == {"Partition 2"}

    tracker.update_scheduled_partitions("Partition 2")
    assert tracker.get_scheduled_partitions() == {"Partition 1", "Partition 2"}
    partitions_to_optimize = tracker.get_partitions_to_optimize()
    # Check that we don't return None but a set whose length is 0 indicating
    # that all optimizations have been run.
    assert partitions_to_optimize is not None
    assert len(partitions_to_optimize) == 0

    tracker.delete_all_states()
    assert len(tracker.get_all_partitions()) == 0
    assert len(tracker.get_scheduled_partitions()) == 0


def test_run_optimize_with_partition_tracker() -> None:
    def write_error_message(writable_storage: WritableTableStorage, time: int) -> None:
        write_processed_messages(
            writable_storage,
            [
                InsertBatch(
                    [
                        {
                            "event_id": str(uuid.uuid4()),
                            "project_id": 1,
                            "deleted": 0,
                            "timestamp": time,
                            "retention_days": settings.DEFAULT_RETENTION_DAYS,
                        }
                    ],
                    None,
                ),
            ],
        )

    storage = get_writable_storage(StorageKey.ERRORS)
    cluster = storage.get_cluster()
    clickhouse_pool = cluster.get_query_connection(ClickhouseClientSettings.OPTIMIZE)
    table = storage.get_table_writer().get_schema().get_local_table_name()
    database = cluster.get_database()
    tracker = OptimizedPartitionTracker(
        redis_client=redis_client,
        host=cluster.get_host(),
        port=cluster.get_port(),
        database=database,
        table=table,
        expire_time=(datetime.now() + timedelta(minutes=3)),
    )

    # Write some messages to the database
    for week in range(0, 4):
        write_error_message(
            writable_storage=storage,
            time=int((datetime.now() - timedelta(weeks=week)).timestamp()),
        )
        write_error_message(
            writable_storage=storage,
            time=int((datetime.now() - timedelta(weeks=week)).timestamp()),
        )

    partitions = optimize.get_partitions_to_optimize(
        clickhouse_pool, storage, database, table
    )

    original_num_partitions = len(partitions)
    assert original_num_partitions > 0
    assert len(tracker.get_all_partitions()) == 0
    assert len(tracker.get_scheduled_partitions()) == 0

    # Mark the partitions as optimized in partition tracker to test behavior.
    tracker.update_all_partitions([partition.name for partition in partitions])
    for partition in partitions:
        tracker.update_all_partitions([partition.name])
        tracker.update_scheduled_partitions(partition.name)

    tracker_completed_partitions = tracker.get_scheduled_partitions()
    assert tracker_completed_partitions is not None
    assert len(tracker_completed_partitions) == original_num_partitions

    num_optimized = run_optimize_cron_job(
        clickhouse=clickhouse_pool,
        storage=storage,
        database=database,
        parallel=1,
        clickhouse_host="localhost",
        tracker=tracker,
    )
    assert num_optimized == 0

    # Fix the optimized partition tracker and run_optimize again.
    # Now we should optimize all the partitions.
    tracker.delete_all_states()
    num_optimized = run_optimize_cron_job(
        clickhouse=clickhouse_pool,
        storage=storage,
        database=database,
        parallel=1,
        clickhouse_host="localhost",
        tracker=tracker,
    )
    assert num_optimized == original_num_partitions


def test_run_optimize_with_ongoing_merges() -> None:
    def write_error_message(writable_storage: WritableTableStorage, time: int) -> None:
        write_processed_messages(
            writable_storage,
            [
                InsertBatch(
                    [
                        {
                            "event_id": str(uuid.uuid4()),
                            "project_id": 1,
                            "deleted": 0,
                            "timestamp": time,
                            "retention_days": settings.DEFAULT_RETENTION_DAYS,
                        }
                    ],
                    None,
                ),
            ],
        )

    storage = get_writable_storage(StorageKey.ERRORS)
    cluster = storage.get_cluster()
    clickhouse_pool = cluster.get_query_connection(ClickhouseClientSettings.OPTIMIZE)
    table = storage.get_table_writer().get_schema().get_local_table_name()
    database = cluster.get_database()
    tracker = OptimizedPartitionTracker(
        redis_client=redis_client,
        host=cluster.get_host(),
        port=cluster.get_port(),
        database=database,
        table=table,
        expire_time=(datetime.now() + timedelta(minutes=3)),
    )
    tracker.delete_all_states()

    # Write some messages to the database
    for week in range(0, 4):
        write_error_message(
            writable_storage=storage,
            time=int((datetime.now() - timedelta(weeks=week)).timestamp()),
        )
        write_error_message(
            writable_storage=storage,
            time=int((datetime.now() - timedelta(weeks=week)).timestamp()),
        )

    partitions = optimize.get_partitions_to_optimize(
        clickhouse_pool, storage, database, table
    )

    original_num_partitions = len(partitions)
    assert original_num_partitions > 0
    assert len(tracker.get_all_partitions()) == 0
    assert len(tracker.get_scheduled_partitions()) == 0

    with patch.object(optimize, "get_current_large_merges") as mock_merge_ids:

        # mock ongoing merges on half the partitions
        current_merges = [
            util.MergeInfo(
                "90-20220613_0_1216096_1417",
                10,
                0.5,
                60_000_000_000,
            ),
        ]
        # mock_merge_ids.return_value = current_merges
        mock_merge_ids.side_effect = [
            current_merges,
            [],
            current_merges,
            [],
            [],
            [],
        ]  # first & thid call returns ongoing merges, rest return no ongoing merges

        with patch.object(time, "sleep") as sleep_mock:
            num_optimized = run_optimize_cron_job(
                clickhouse=clickhouse_pool,
                storage=storage,
                database=database,
                parallel=1,
                clickhouse_host="localhost",
                tracker=tracker,
            )
            assert num_optimized == original_num_partitions
            assert mock_merge_ids.call_count == 6

            sleep_mock.assert_called_with(
                settings.OPTIMIZE_BASE_SLEEP_TIME + current_merges[0].estimated_time
            )
            sleep_mock.call_count = 4  # twice for first and second patitition


def test_merge_info() -> None:
    merge_query_result = ClickhouseResult(
        results=[
            [
                "90-20220613_0_1216096_1417",
                8020.61436897,
                0.9895385071013121,
                40_000_000_000,
            ],
            ["90-20220912_133168_133172_1", 0.181636831, 1.0, 60_000_000_000],
        ]
    )

    with patch.object(ClickhousePool, "execute") as mock_clickhouse_execute:
        mock_clickhouse_execute.return_value = merge_query_result
        merge_info = optimize.get_current_large_merges(
            clickhouse=ClickhousePool(
                "localhost", 9000, "user", "password", "database"
            ),
            database="default",
            table="errors_local",
        )
        assert merge_info == [
            util.MergeInfo(
                "90-20220613_0_1216096_1417",
                8020.61436897,
                0.9895385071013121,
                40_000_000_000,
            ),
            util.MergeInfo(
                "90-20220912_133168_133172_1",
                0.181636831,
                1.0,
                60_000_000_000,
            ),
        ]

        assert merge_info[0].estimated_time == 8020.61436897 / (
            0.9895385071013121 + 0.0001
        )
        busy, sleep_time = optimize.is_busy_merging(
            clickhouse=ClickhousePool(
                "localhost", 9000, "user", "password", "database"
            ),
            database="default",
            table="errors_local",
        )
        assert busy
        assert sleep_time == merge_info[0].estimated_time
