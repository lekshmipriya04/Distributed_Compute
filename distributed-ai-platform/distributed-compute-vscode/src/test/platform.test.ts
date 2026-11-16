import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  archiveUrl,
  composeFailureHint,
  isStartupRace,
  servicesWithoutNetwork,
  composeInvocation,
  dockerLaunchCommand,
  dockerStartHint,
  dockerStateFrom,
  normalizeLineEndings,
  PLATFORM_RELEASE,
  platformTag,
  startupAction,
  withDockerPath,
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
  it('downloads the source archive of the platform release the extension targets', () => {
    expect(PLATFORM_RELEASE).toMatch(/^\d+\.\d+\.\d+$/);
    const manifest = JSON.parse(readFileSync(join(__dirname, '..', '..', 'package.json'), 'utf8'));
    expect(PLATFORM_RELEASE).toBe(manifest.version); // released together
    expect(platformTag('1.0.1')).toBe('v1.0.1');
    expect(platformTag('v1.0.1')).toBe('v1.0.1');
    expect(archiveUrl('1.1.0')).toBe(
      'https://codeload.github.com/lekshmipriya04/Distributed_Compute/tar.gz/refs/tags/v1.1.0',
    );
    expect(archiveUrl(undefined)).toBe('https://codeload.github.com/lekshmipriya04/Distributed_Compute/tar.gz/refs/heads/main');
  });
});

describe('docker PATH', () => {
  it("appends Docker Desktop's folders without reordering the user's PATH", () => {
    const env = withDockerPath('darwin', { PATH: '/opt/homebrew/bin:/usr/bin', HOME: '/Users/me' });
    expect(env.PATH).toBe(
      '/opt/homebrew/bin:/usr/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:/Users/me/.docker/bin',
    );
  });

  it('keeps the Windows "Path" variable name', () => {
    const env = withDockerPath('win32', { Path: 'C:\\Windows', ProgramFiles: 'C:\\Program Files' });
    expect(env.Path).toBe('C:\\Windows;C:\\Program Files\\Docker\\Docker\\resources\\bin');
    expect(env.PATH).toBeUndefined();
  });
});

describe('compose failure hints', () => {
  it('names the busy port', () => {
    const out =
      'Error response from daemon: driver failed programming external connectivity on endpoint x: Bind for 127.0.0.1:8090 failed: port is already allocated';
    expect(composeFailureHint(out)).toMatch(/Port 8090 is already used/);
    expect(composeFailureHint('listen tcp 0.0.0.0:3001: bind: address already in use')).toMatch(/Port 3001/);
  });

  it('recognises the common Docker failures', () => {
    expect(composeFailureHint('invalid pool request: Pool overlaps with other one on this address space')).toMatch(/docker network prune/);
    expect(composeFailureHint('Conflict. The container name "/postgres" is already in use by container "abc"')).toMatch(/docker rm -f postgres/);
    expect(composeFailureHint('write /var/lib/docker/x: no space left on device')).toMatch(/disk space/);
    expect(
      composeFailureHint('error getting credentials - err: exec: "docker-credential-desktop": executable file not found in $PATH'),
    ).toMatch(/saved login/);
    expect(composeFailureHint('failed to resolve source metadata for docker.io/library/python:3.11-slim: i/o timeout')).toMatch(/internet/);
    expect(composeFailureHint('dependency failed to start: container x-postgres-1 is unhealthy')).toMatch(/stopped while starting/);
    expect(composeFailureHint('something new')).toBeUndefined();
  });
});

describe('docker start race', () => {
  it('retries only when a dependency was not ready yet', () => {
    expect(isStartupRace('dependency failed to start: container x-ssh-executor-1 is unhealthy')).toBe(true);
    expect(isStartupRace('Bind for 127.0.0.1:8090 failed: port is already allocated')).toBe(false);
  });

  it('finds containers that lost their network', () => {
    const out = 'postgres 1\nextension-gateway 0\napi-gateway 0\r\nfrontend 0\nssh-executor 1\n\n';
    expect(servicesWithoutNetwork(out)).toEqual(['extension-gateway', 'api-gateway', 'frontend']);
    expect(servicesWithoutNetwork('postgres 1\n')).toEqual([]);
  });
});
