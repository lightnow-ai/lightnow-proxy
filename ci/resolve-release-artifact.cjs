// Select native Actions evidence for this exact tag; never rebuild on failure.
module.exports = async ({ github, context, core, wait = ms => new Promise(resolve => setTimeout(resolve, ms)) }) => {
  const { owner, repo } = context.repo;
  for (let attempt = 0; attempt < 90; attempt++) {
    const { data } = await github.rest.actions.listWorkflowRuns({
      owner, repo, workflow_id: 'ci.yml', branch: 'main',
      event: 'push', head_sha: context.sha, per_page: 100,
    });
    const run = data.workflow_runs.find(run => run.head_sha === context.sha && run.head_branch === 'main' && run.event === 'push');
    if (run?.status === 'completed') {
      if (run.conclusion !== 'success') throw new Error(`Exact source CI ${run.html_url} finished ${run.conclusion}`);
      core.setOutput('run-id', run.id);
      core.info(`Reusing ${run.html_url} for ${context.sha}`);
      return;
    }
    await wait(10000);
  }
  throw new Error('No successful exact-revision main CI; rebuild fallback is forbidden');
};
