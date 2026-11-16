// Pure decisions for running the platform on this machine with Docker, kept
// free of the vscode API so they can be unit tested.

/** Where the extension downloads the platform when the user has no copy. */
export const PLATFORM_REPO = 'https://github.com/lekshmipriya04/Distributed_Compute';

/**
 * The platform release this extension version was tested with; the extension
 * downloads that tag. Both share one version number, so this moves with every
 * release.
 */
export const PLATFORM_RELEASE = '1.1.0';

/** Git tag of a platform release. */
export function platformTag(release: string): string {
  return `v${release.replace(/^v/, '')}`;
}

/**
 * Source archive (.tar.gz) of that release, or of the default branch when none
 * is given. An archive needs no Git on the machine (often missing on Windows)
 * and keeps LF line endings whatever Git's autocrlf setting is.
 */
export function archiveUrl(release: string | undefined): string {
  const ref = release ? `refs/tags/${platformTag(release)}` : 'refs/heads/main';
  return `${PLATFORM_REPO.replace('https://github.com/', 'https://codeload.github.com/')}/tar.gz/${ref}`;
}

/** The platform's docker-compose.yml lives in this folder of the repository. */
export const PLATFORM_SUBDIR = 'distributed-ai-platform';

export type StartupAction =
  /** The local gateway answers: connect to it. */
  | 'connect'
  /** The platform was set up before but is not running: start it. */
  | 'start'
  /** Never set up here: offer the one-click setup. */
  | 'offer-setup'
  /** Remote master, or the user said no: do what the settings say. */
  | 'none';

export interface StartupFacts {
  masterIsLocal: boolean;
  gatewayHealthy: boolean;
  /** A platform folder is known (set up before, or open in the workspace). */
  platformKnown: boolean;
  autoStart: boolean;
  setupDismissed: boolean;
}

export function startupAction(f: StartupFacts): StartupAction {
  if (!f.masterIsLocal) {
    return 'none';
  }
  if (f.gatewayHealthy) {
    return 'connect';
  }
  if (f.platformKnown) {
    return f.autoStart ? 'start' : 'none';
  }
  return f.setupDismissed ? 'none' : 'offer-setup';
}

/**
 * Docker's state from `docker info`: no CLI at all, a daemon that is not
 * running, or (Linux) a daemon this user may not use.
 */
export type DockerState = 'running' | 'stopped' | 'missing' | 'no-permission';

export function dockerStateFrom(result: { spawnFailed: boolean; exitCode: number; output?: string }): DockerState {
  if (result.spawnFailed) {
    return 'missing';
  }
  if (result.exitCode === 0) {
    return 'running';
  }
  return /permission denied/i.test(result.output ?? '') ? 'no-permission' : 'stopped';
}

export interface LaunchCommand {
  cmd: string;
  args: string[];
  /** Started detached: Docker Desktop keeps running after we return. */
  detached: boolean;
}

/** How to start Docker on this OS, or undefined when we cannot. */
export function dockerLaunchCommand(
  platform: NodeJS.Platform,
  env: Record<string, string | undefined> = {},
): LaunchCommand | undefined {
  switch (platform) {
    case 'darwin':
      return { cmd: 'open', args: ['-a', 'Docker'], detached: false };
    case 'win32': {
      const programFiles = env.ProgramFiles || 'C:\\Program Files';
      return { cmd: `${programFiles}\\Docker\\Docker\\Docker Desktop.exe`, args: [], detached: true };
    }
    case 'linux':
      // Docker Desktop for Linux; Docker Engine needs root (see dockerStartHint).
      return { cmd: 'systemctl', args: ['--user', 'start', 'docker-desktop'], detached: false };
    default:
      return undefined;
  }
}

/** What to tell the user when Docker could not be started for them. */
export function dockerStartHint(platform: NodeJS.Platform, state: DockerState): string {
  if (state === 'no-permission') {
    return 'Docker is running, but this user may not use it. Run "sudo usermod -aG docker $USER", log out and back in, then try again.';
  }
  if (platform === 'linux') {
    return 'Start Docker ("sudo systemctl start docker", or open Docker Desktop), then run "Set Up Platform" again.';
  }
  return 'Open Docker Desktop yourself, wait until it says it is running, then run "Set Up Platform" again.';
}

/** Arguments for Compose v2 ("docker compose ...") or v1 ("docker-compose ..."). */
export function composeInvocation(v2: boolean, args: string[]): { cmd: string; args: string[] } {
  return v2 ? { cmd: 'docker', args: ['compose', ...args] } : { cmd: 'docker-compose', args };
}

/**
 * Source sent to workers uses LF line endings: a file saved with CRLF on
 * Windows breaks shell scripts on Linux workers ("$'\r': command not found").
 */
export function normalizeLineEndings(code: string): string {
  return code.replace(/\r\n?/g, '\n');
}

/**
 * Folders Docker Desktop installs its CLI and helpers into. VS Code started
 * from the Dock, Start menu or a desktop launcher may not have them on PATH,
 * so "docker" is not found, or a pull fails with "docker-credential-desktop:
 * executable file not found in $PATH".
 */
export function dockerPathDirs(platform: NodeJS.Platform, env: Record<string, string | undefined>): string[] {
  const home = env.HOME || env.USERPROFILE || '';
  switch (platform) {
    case 'darwin':
      return ['/usr/local/bin', '/opt/homebrew/bin', '/Applications/Docker.app/Contents/Resources/bin', `${home}/.docker/bin`];
    case 'win32':
      return [`${env.ProgramFiles || 'C:\\Program Files'}\\Docker\\Docker\\resources\\bin`];
    case 'linux':
      return ['/usr/local/bin', '/usr/bin', `${home}/.docker/bin`];
    default:
      return [];
  }
}

/** A copy of env whose PATH also holds Docker's folders (appended, so the user's PATH wins). */
export function withDockerPath(
  platform: NodeJS.Platform,
  env: Record<string, string | undefined>,
): Record<string, string | undefined> {
  // Windows keeps the variable as "Path"; names are case-insensitive there.
  const key = platform === 'win32' ? (Object.keys(env).find((k) => k.toUpperCase() === 'PATH') ?? 'Path') : 'PATH';
  const sep = platform === 'win32' ? ';' : ':';
  const parts = (env[key] ?? '').split(sep).filter((p) => p !== '');
  const extra = dockerPathDirs(platform, env).filter((d) => !parts.includes(d));
  return { ...env, [key]: [...parts, ...extra].join(sep) };
}

/**
 * A plain-language cause and fix for a failed `docker compose up`, from its
 * output; undefined when the cause is not one we recognise.
 */
export function composeFailureHint(output: string): string | undefined {
  const port = /(?:port is already allocated|address already in use|ports are not available|only one usage of each socket address)/i;
  if (port.test(output)) {
    const p = /(?:\d{1,3}(?:\.\d{1,3}){3}|\[?::1?\]?):(\d{2,5})/.exec(output)?.[1];
    return `Port ${p ?? 'needed by the platform'} is already used by another program or Docker container on this computer. Stop it, then run "Set Up Platform" again.`;
  }
  if (/pool overlaps|non-overlapping ipv4 address pool/i.test(output)) {
    return 'Docker has no free network address range left. Run "docker network prune" to remove unused networks, then run "Set Up Platform" again.';
  }
  const name = /container name "\/?([^"]+)" is already in use/i.exec(output);
  if (name) {
    return `An old container named "${name[1]}" is in the way. Run "docker rm -f ${name[1]}", then run "Set Up Platform" again.`;
  }
  if (/no space left on device/i.test(output)) {
    return 'Docker is out of disk space. Run "docker system prune" (or give Docker Desktop more disk), then run "Set Up Platform" again.';
  }
  if (/docker-credential-\S+.*(not found|not installed)|error getting credentials/i.test(output)) {
    return 'Docker could not read its saved login. Restart VS Code after Docker Desktop is running; if that does not help, remove the "credsStore" line from ~/.docker/config.json.';
  }
  if (/cannot connect to the docker daemon|docker daemon is not running|error during connect/i.test(output)) {
    return 'Docker stopped while the platform was starting. Open Docker Desktop, wait until it is running, then run "Set Up Platform" again.';
  }
  if (/tls handshake timeout|i\/o timeout|no such host|temporary failure in name resolution|toomanyrequests|failed to resolve source metadata|read: connection reset/i.test(output)) {
    return 'Docker could not download what the platform needs. Check the internet connection (and any proxy in Docker Desktop\'s settings), then run "Set Up Platform" again.';
  }
  if (/is unhealthy|dependency failed to start|exited with code/i.test(output)) {
    return 'A platform service stopped while starting. Its log is in the Distributed Compute output.';
  }
  return undefined;
}

export const DOCKER_DOWNLOAD_URL = 'https://www.docker.com/products/docker-desktop/';
