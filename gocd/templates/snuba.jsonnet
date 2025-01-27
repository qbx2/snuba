local snuba = import './pipelines/snuba.libsonnet';
local pipedream = import 'github.com/getsentry/gocd-jsonnet/v1.0.0/pipedream.libsonnet';

local pipedream_config = {
  name: 'snuba-next',
  materials: {
    snuba_repo: {
      git: 'git@github.com:getsentry/snuba.git',
      shallow_clone: true,
      branch: 'master',
      destination: 'snuba',
    },
  },

  // Set to true to auto-deploy changes (defaults to true)
  auto_deploy: false,
  // Set to true if you want each pipeline to require manual approval
  auto_pipeline_progression: false,
};

pipedream.render(pipedream_config, snuba)
