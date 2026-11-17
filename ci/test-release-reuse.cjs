const assert = require('node:assert/strict');
const test = require('node:test');
const resolve = require('./resolve-release-artifact.cjs');
const good = { id: 7, head_sha: 'exact', head_branch: 'main', event: 'push', status: 'completed', conclusion: 'success' };

async function check(responses) {
  let calls = 0;
  const outputs = [];
  const github = { rest: { actions: { listWorkflowRuns: async options => {
    assert.equal(options.head_sha, 'exact');
    assert.equal(options.branch, 'main');
    return { data: { workflow_runs: responses[Math.min(calls++, responses.length - 1)] } };
  } } } };
  const core = { setOutput: (key, value) => outputs.push([key, value]), info: () => {} };
  await resolve({ github, context: { repo: { owner: 'lightnow-ai', repo: 'client' }, sha: 'exact' }, core, wait: async () => {} });
  assert.deepEqual(outputs, [['run-id', 7]]);
}

test('reuse exact successful native main run', () => check([[good]]));
test('wait for the same source run to finish', () => check([[{ ...good, status: 'in_progress' }], [good]]));
for (const changed of [{ head_sha: 'other' }, { head_branch: 'other' }, { event: 'pull_request' }, { conclusion: 'failure' }]) {
  test(`reject incompatible source evidence ${JSON.stringify(changed)}`, async () => {
    await assert.rejects(check([[{ ...good, ...changed }]]));
  });
}
test('missing native evidence never triggers rebuilding', async () => {
  await assert.rejects(check([[]]));
});
