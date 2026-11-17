const expected = Number(process.env.ALLURE_EXPECTED_PASSED);
if (!Number.isSafeInteger(expected) || expected < 1) throw new Error('Verified pytest inventory is required');
export default {
  name: 'LightNow native client source tests',
  output: 'test-reports/allure-report',
  plugins: { awesome: { options: { reportLanguage: 'en' } } },
  qualityGate: { rules: [
    { id: 'required-outcomes', maxFailures: 0 },
    { id: 'required-passed-count', filter: (test) => test.status === 'passed', minTestsCount: expected },
  ] },
};
