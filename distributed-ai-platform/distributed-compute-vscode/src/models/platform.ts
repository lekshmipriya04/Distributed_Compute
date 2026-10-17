// Pure decisions for running the platform on this machine with Docker, kept
// free of the vscode API so they can be unit tested.

/** Where the extension downloads the platform when the user has no copy. */
export const PLATFORM_REPO = 'https://github.com/lekshmipriya04/Distributed_Compute.git';

/**
 * The platform release this extension version was tested with; the extension
 * downloads that tag. Since 2.0.0 both share one version number, so this
 * moves with every release.
 */
export const PLATFORM_RELEASE = '2.0.0';

/** Git tag of a platform release. */
export function platformTag(release: string): string {
  return `v${release.replace(/^v/, '')}`;
}

/** `git clone` of that release (or of the default branch when none is given). */
export function cloneArgs(release: string | undefined, target: string): string[] {
  const ref = release ? ['--branch', platformTag(release)] : [];
  return ['clone', '--depth', '1', ...ref, PLATFORM_REPO, target];
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

export const DOCKER_DOWNLOAD_URL = 'https://www.docker.com/products/docker-desktop/';
