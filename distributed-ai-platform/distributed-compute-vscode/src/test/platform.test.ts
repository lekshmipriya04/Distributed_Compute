import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  cloneArgs,
  composeInvocation,
  dockerLaunchCommand,
  dockerStartHint,
  dockerStateFrom,
  normalizeLineEndings,
  PLATFORM_RELEASE,
  platformTag,
  startupAction,
  type StartupFacts,
} from '../models/platform';

const base: StartupFacts = {
  masterIsLocal: true,
  gatewayHealthy: false,
  platformKnown: false,
  autoStart: true,
  setupDismissed: false,
};

describe('startupAction', () => {
  it('leaves a remote master to the autoConnect setting', () => {
    expect(startupAction({ ...base, masterIsLocal: false, gatewayHealthy: true })).toBe('none');
  });

  it('connects when the local gateway already answers', () => {
    expect(startupAction({ ...base, gatewayHealthy: true })).toBe('connect');
  });

  it('starts a platform that was set up before', () => {
    expect(startupAction({ ...base, platformKnown: true })).toBe('start');
  });

  it('does not start containers when auto start is off', () => {
    expect(startupAction({ ...base, platformKnown: true, autoStart: false })).toBe('none');
  });

  it('offers setup on a fresh install, unless dismissed', () => {
    expect(startupAction(base)).toBe('offer-setup');
    expect(startupAction({ ...base, setupDismissed: true })).toBe('none');
  });
});

describe('docker helpers', () => {
  it('reads docker info outcomes', () => {
    expect(dockerStateFrom({ spawnFailed: true, exitCode: -1 })).toBe('missing');
    expect(dockerStateFrom({ spawnFailed: false, exitCode: 0 })).toBe('running');
    expect(dockerStateFrom({ spawnFailed: false, exitCode: 1, output: 'Cannot connect to the Docker daemon' })).toBe('stopped');
    expect(
      dockerStateFrom({
        spawnFailed: false,
        exitCode: 1,
        output: 'permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock',
      }),
    ).toBe('no-permission');
  });

  it('knows how to start Docker on each OS', () => {
    expect(dockerLaunchCommand('darwin')).toEqual({ cmd: 'open', args: ['-a', 'Docker'], detached: false });
    expect(dockerLaunchCommand('win32', {})).toEqual({
      cmd: 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe',
      args: [],
      detached: true,
    });
    expect(dockerLaunchCommand('win32', { ProgramFiles: 'D:\\Apps' })?.cmd).toBe('D:\\Apps\\Docker\\Docker\\Docker Desktop.exe');
    expect(dockerLaunchCommand('linux')?.cmd).toBe('systemctl');
    expect(dockerLaunchCommand('aix')).toBeUndefined();
  });

  it('gives an OS-specific hint when Docker cannot be started', () => {
    expect(dockerStartHint('linux', 'no-permission')).toMatch(/usermod -aG docker/);
    expect(dockerStartHint('linux', 'stopped')).toMatch(/systemctl start docker/);
    expect(dockerStartHint('win32', 'stopped')).toMatch(/Docker Desktop/);
  });

  it('falls back to docker-compose v1', () => {
    expect(composeInvocation(true, ['up', '-d'])).toEqual({ cmd: 'docker', args: ['compose', 'up', '-d'] });
    expect(composeInvocation(false, ['up', '-d'])).toEqual({ cmd: 'docker-compose', args: ['up', '-d'] });
  });
});

describe('line endings', () => {
  it('sends LF to workers whatever the editor saved', () => {
    expect(normalizeLineEndings('read n\r\necho $n\r\n')).toBe('read n\necho $n\n');
    expect(normalizeLineEndings('old\rmac')).toBe('old\nmac');
    expect(normalizeLineEndings('unix\n')).toBe('unix\n');
  });
});

describe('platform download', () => {
  it('clones the tag of the platform release the extension targets', () => {
    expect(PLATFORM_RELEASE).toMatch(/^\d+\.\d+\.\d+$/);
    const manifest = JSON.parse(readFileSync(join(__dirname, '..', '..', 'package.json'), 'utf8'));
    expect(PLATFORM_RELEASE).toBe(manifest.version); // released together
    expect(platformTag('1.0.1')).toBe('v1.0.1');
    expect(platformTag('v1.0.1')).toBe('v1.0.1');
    expect(cloneArgs('1.0.1', '/tmp/p')).toEqual([
      'clone', '--depth', '1', '--branch', 'v1.0.1',
      'https://github.com/lekshmipriya04/Distributed_Compute.git', '/tmp/p',
    ]);
    expect(cloneArgs(undefined, '/tmp/p')).not.toContain('--branch');
  });
});
