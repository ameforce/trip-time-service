import { execFileSync } from 'node:child_process';
import { mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

import { writeLiveSummary } from './live-summary.mjs';

type ProcessInfo = {
  ProcessId: number;
  Name: string;
  CommandLine: string | null;
};

function escapePowerShell(value: string): string {
  return value.replace(/'/g, "''");
}

function readProcessesForProfile(profilePath: string): ProcessInfo[] {
  if (process.platform !== 'win32') {
    return readProcessesForProfileWithPs(profilePath);
  }

  const script = `
    $target = '${escapePowerShell(profilePath)}'
    $items = Get-CimInstance Win32_Process |
      Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*$target*" -and
        ($_.Name -match 'chrome|chromedriver|chromium')
      } |
      Select-Object ProcessId, Name, CommandLine
    $items | ConvertTo-Json -Compress
  `;
  const raw = execFileSync('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf-8',
  }).trim();
  if (!raw) {
    return [];
  }
  const parsed = JSON.parse(raw) as ProcessInfo | ProcessInfo[];
  return Array.isArray(parsed) ? parsed : [parsed];
}

function readProcessesForProfileWithPs(profilePath: string): ProcessInfo[] {
  try {
    const raw = execFileSync('ps', ['-eo', 'pid=,comm=,args='], { encoding: 'utf-8' });
    return raw
      .split('\n')
      .map((line) => line.match(/^\s*(\d+)\s+(\S+)\s+(.*)$/))
      .filter((match): match is RegExpMatchArray => Boolean(match))
      .filter((match) => match[0].includes(profilePath) && /chrome|chromedriver|chromium/i.test(match[2]))
      .map((match) => ({
        ProcessId: Number(match[1]),
        Name: basename(match[2]),
        CommandLine: match[3],
      }));
  } catch {
    return [];
  }
}

async function globalTeardown(): Promise<void> {
  const artifactRoot = process.env.TTS_E2E_ARTIFACTS_DIR
    ? resolve(process.env.TTS_E2E_ARTIFACTS_DIR)
    : resolve('.artifacts/live');
  mkdirSync(artifactRoot, { recursive: true });

  const profilePath = process.env.TTS_E2E_CHROME_USER_DATA_DIR ?? '';
  const deadline = Date.now() + 15_000;
  let leakedProcesses: ProcessInfo[] = [];
  const strictLeakProbe = process.env.TTS_E2E_STRICT_LEAK_PROBE === '1';

  while (Date.now() <= deadline) {
    leakedProcesses = profilePath ? readProcessesForProfile(profilePath) : [];
    if (leakedProcesses.length === 0) {
      break;
    }
    await sleep(1_000);
  }

  writeFileSync(
    resolve(artifactRoot, 'shutdown-leak-report.json'),
    JSON.stringify(
        {
          checked_at: new Date().toISOString(),
          profile_path: profilePath,
          leaked_processes: leakedProcesses,
          strict_blocking: strictLeakProbe,
          note:
            leakedProcesses.length > 0
              ? 'Playwright globalTeardown may run before webServer shutdown; ' +
                'use tests/test_shutdown_resilience.py for authoritative shutdown validation.'
              : null,
        },
        null,
        2,
      ),
    'utf-8',
  );

  writeFileSync(
    resolve(artifactRoot, 'suite-artifacts.json'),
    JSON.stringify(
      {
        generated_at: new Date().toISOString(),
        test_reports: collectJsonReports(resolve(artifactRoot, 'test-results')),
        shutdown_report: resolve(artifactRoot, 'shutdown-leak-report.json'),
      },
      null,
      2,
    ),
    'utf-8',
  );

  writeLiveSummary(artifactRoot);

  if (strictLeakProbe && leakedProcesses.length > 0) {
    throw new Error(
      `chromium cleanup leak detected: ${JSON.stringify(leakedProcesses)}`,
    );
  }
}

function collectJsonReports(rootPath: string): string[] {
  try {
    return walkJsonFiles(rootPath);
  } catch {
    return [];
  }
}

function walkJsonFiles(rootPath: string): string[] {
  const entries = readdirSync(rootPath, { withFileTypes: true });
  const results: string[] = [];
  for (const entry of entries) {
    const absolutePath = join(rootPath, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkJsonFiles(absolutePath));
      continue;
    }
    if (entry.isFile() && absolutePath.endsWith('.json') && statSync(absolutePath).size > 0) {
      results.push(absolutePath);
    }
  }
  return results.sort();
}

export default globalTeardown;
