import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const LIVE_BUCKETS = [
  'ncaptcha_backoff',
  'panel_parse_timeout',
  'provider_retry_exhausted',
  'coords_unresolved',
  'stream_stall_timeout',
  'environment_unavailable',
  'unknown',
];

export function writeLiveSummary(artifactRoot) {
  const runtimePath = resolve(artifactRoot, 'e2e-runtime.json');
  const runtime = readJsonObject(runtimePath);
  const live = runtime.live === true;
  const fixture = runtime.fixture === true;
  const reportPaths = collectJsonReports(resolve(artifactRoot, 'test-results'));
  const lastRun = readJsonObject(resolve(artifactRoot, 'test-results', '.last-run.json'));
  const failedTests = Array.isArray(lastRun.failedTests)
    ? lastRun.failedTests.map((value) => String(value))
    : [];
  const bucketCounts = Object.fromEntries(
    LIVE_BUCKETS.map((bucket) => [bucket, 0]),
  );
  const classifiedBuckets = live
    ? collectFailureEvidence(artifactRoot, failedTests).map(classifyFailureBucket)
    : [];
  for (const bucket of classifiedBuckets) {
    bucketCounts[bucket] += 1;
  }
  if (live && reportPaths.length === 0) {
    bucketCounts.environment_unavailable += 1;
  }

  const failedCount =
    Number(process.env.TTS_LIVE_FAILED_COUNT || '') || failedTests.length;
  const runStatus = String(lastRun.status || '');
  const summary = {
    generated_at: new Date().toISOString(),
    mode:
      String(runtime.TTS_LIVE_MODE || '') ||
      process.env.TTS_LIVE_MODE ||
      (live ? 'smoke' : 'fixture'),
    baseURL: String(runtime.baseURL || process.env.E2E_BASE_URL || ''),
    provider: String(runtime.TTS_PROVIDER || process.env.TTS_PROVIDER || 'unknown'),
    fixture,
    strict: runtime.strict === true || process.env.TTS_PORT_STRICT === '1',
    policy:
      String(runtime.LIVE_E2E_POLICY || '') ||
      process.env.LIVE_E2E_POLICY ||
      'advisory',
    status: live
      ? runStatus === 'failed' || failedCount > 0
        ? 'failed'
        : reportPaths.length > 0
          ? 'completed'
          : 'no_test_reports'
      : 'not_live',
    failed_count: failedCount,
    bucket_counts: bucketCounts,
    first_failure_bucket:
      classifiedBuckets[0] ||
      (live && reportPaths.length === 0 ? 'environment_unavailable' : null),
    report_paths: reportPaths.map((reportPath) =>
      safeReportReference(artifactRoot, reportPath),
    ),
  };

  writeFileSync(
    resolve(artifactRoot, 'e2e-live-summary.json'),
    `${JSON.stringify(summary, null, 2)}\n`,
    'utf-8',
  );
  return summary;
}

export function classifyFailureBucket(failureText) {
  const normalized = String(failureText || '').toLowerCase();
  if (normalized.includes('ncaptcha') || normalized.includes('captcha')) {
    return 'ncaptcha_backoff';
  }
  if (
    normalized.includes('coords_unresolved') ||
    normalized.includes('coords unresolved')
  ) {
    return 'coords_unresolved';
  }
  if (
    normalized.includes('stream_stall_timeout') ||
    normalized.includes('stream stall')
  ) {
    return 'stream_stall_timeout';
  }
  if (
    normalized.includes('panel_parse_timeout') ||
    normalized.includes('panel parse') ||
    normalized.includes('autocomplete')
  ) {
    return 'panel_parse_timeout';
  }
  if (
    normalized.includes('provider_retry_exhausted') ||
    normalized.includes('provider retry') ||
    normalized.includes('교통 정보 제공자') ||
    normalized.includes('recommendation') ||
    normalized.includes('road-address-route') ||
    normalized.includes('arrival-mode') ||
    normalized.includes('departure-mode') ||
    normalized.includes('route') ||
    normalized.includes('arrival') ||
    normalized.includes('departure')
  ) {
    return 'provider_retry_exhausted';
  }
  if (
    normalized.includes('environment_unavailable') ||
    normalized.includes('browser') ||
    normalized.includes('webserver') ||
    normalized.includes('network')
  ) {
    return 'environment_unavailable';
  }
  return 'unknown';
}

function readJsonObject(filePath) {
  try {
    return JSON.parse(readFileSync(filePath, 'utf-8'));
  } catch {
    return {};
  }
}

function collectFailureEvidence(artifactRoot, failedTests) {
  const contexts = collectErrorContexts(resolve(artifactRoot, 'test-results'));
  if (contexts.length === 0) {
    return failedTests;
  }
  const evidence = contexts.map(
    ({ filePath, text }) =>
      `${relative(artifactRoot, filePath)}\n${text.slice(0, 2_000)}`,
  );
  if (evidence.length >= failedTests.length) {
    return evidence.slice(0, failedTests.length);
  }
  return evidence.concat(failedTests.slice(evidence.length));
}

function collectErrorContexts(rootPath) {
  try {
    return walkErrorContexts(rootPath);
  } catch {
    return [];
  }
}

function walkErrorContexts(rootPath) {
  const entries = readdirSync(rootPath, { withFileTypes: true });
  const results = [];
  for (const entry of entries) {
    const absolutePath = join(rootPath, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkErrorContexts(absolutePath));
      continue;
    }
    if (entry.isFile() && entry.name === 'error-context.md') {
      results.push({
        filePath: absolutePath,
        text: readFileSync(absolutePath, 'utf-8'),
      });
    }
  }
  return results.sort((a, b) => a.filePath.localeCompare(b.filePath));
}

function safeReportReference(artifactRoot, reportPath) {
  const relativePath = relative(artifactRoot, reportPath);
  const hash = createHash('sha256').update(relativePath).digest('hex').slice(0, 12);
  return `${basename(reportPath)}#${hash}`;
}

function collectJsonReports(rootPath) {
  try {
    return walkJsonFiles(rootPath);
  } catch {
    return [];
  }
}

function walkJsonFiles(rootPath) {
  const entries = readdirSync(rootPath, { withFileTypes: true });
  const results = [];
  for (const entry of entries) {
    const absolutePath = join(rootPath, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkJsonFiles(absolutePath));
      continue;
    }
    if (
      entry.isFile() &&
      absolutePath.endsWith('.json') &&
      statSync(absolutePath).size > 0 &&
      basename(absolutePath) !== '.last-run.json'
    ) {
      results.push(absolutePath);
    }
  }
  return results.sort();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  writeLiveSummary(resolve(process.argv[2] || '.artifacts/live'));
}
