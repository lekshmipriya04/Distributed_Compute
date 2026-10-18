// Projects in the browser: pack a folder the user picked into the .tar.gz the
// master accepts. The same rules as the VS Code extension and `dc`: version
// control, caches, environments and earlier outputs are left out.

export const DEFAULT_EXCLUDES: readonly string[] = [
  '.git/', '.hg/', '.svn/', 'node_modules/', '__pycache__/', '.venv/', 'venv/', '.mypy_cache/', '.pytest_cache/',
  '.ruff_cache/', '.ipynb_checkpoints/', '.idea/', '.vscode/', 'distributed-results/', 'outputs/', '*.pyc', '.DS_Store',
];

export const MAX_PROJECT_BYTES = 500 * 1024 * 1024;

function globToRegExp(glob: string): RegExp {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const ch = glob[i];
    if (ch === '*') {
      if (glob[i + 1] === '*') {
        re += '.*';
        i++;
        if (glob[i + 1] === '/') {
          i++;
          re += '(?:/)?';
        }
      } else {
        re += '[^/]*';
      }
    } else if (ch === '?') {
      re += '[^/]';
    } else {
      re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
    }
  }
  return new RegExp(`^${re}$`);
}

/** gitignore-style, simplified: "dir/" at any depth, "name" at any depth, "a/b" from the root. */
export function isExcluded(relPath: string, patterns: readonly string[] = DEFAULT_EXCLUDES): boolean {
  const parts = relPath.split('/');
  for (const raw of patterns) {
    const pattern = raw.trim().replace(/^\.\//, '');
    if (!pattern || pattern.startsWith('#')) continue;
    const dirOnly = pattern.endsWith('/');
    const body = dirOnly ? pattern.slice(0, -1) : pattern;
    const re = globToRegExp(body.replace(/^\//, ''));
    if (body.includes('/')) {
      for (let i = 1; i <= parts.length; i++) {
        if ((!dirOnly || i < parts.length) && re.test(parts.slice(0, i).join('/'))) return true;
      }
    } else if ((dirOnly ? parts.slice(0, -1) : parts).some((name) => re.test(name))) {
      return true;
    }
  }
  return false;
}

export interface ProjectFile {
  path: string;
  data: Uint8Array;
}

/** A likely command for the folder. */
export function guessCommand(paths: readonly string[]): string {
  const top = new Set(paths.filter((p) => !p.includes('/')));
  for (const name of ['train.py', 'main.py', 'run.py', 'app.py']) {
    if (top.has(name)) return `python3 ${name}`;
  }
  if (top.has('run.sh')) return 'sh run.sh';
  if (top.has('Makefile')) return 'make';
  const py = [...top].find((p) => p.endsWith('.py'));
  return py ? `python3 ${py}` : '';
}

const encoder = new TextEncoder();

function put(buf: Uint8Array, offset: number, length: number, value: string): void {
  const bytes = encoder.encode(value);
  buf.set(bytes.subarray(0, length), offset);
}

function octal(buf: Uint8Array, offset: number, length: number, value: number): void {
  put(buf, offset, length, value.toString(8).padStart(length - 1, '0') + '\0');
}

function splitName(path: string): [string, string] {
  if (encoder.encode(path).length <= 100) return [path, ''];
  for (let i = path.lastIndexOf('/'); i > 0; i = path.lastIndexOf('/', i - 1)) {
    const prefix = path.slice(0, i);
    const name = path.slice(i + 1);
    if (encoder.encode(prefix).length <= 155 && encoder.encode(name).length <= 100) return [name, prefix];
  }
  throw new Error(`The path is too long to send: ${path}`);
}

function header(path: string, size: number, mode: number): Uint8Array {
  const h = new Uint8Array(512);
  const [name, prefix] = splitName(path);
  put(h, 0, 100, name);
  octal(h, 100, 8, mode);
  octal(h, 108, 8, 0);
  octal(h, 116, 8, 0);
  octal(h, 124, 12, size);
  octal(h, 136, 12, 0);
  h.fill(32, 148, 156);
  h[156] = 48; // '0': regular file
  put(h, 257, 6, 'ustar\0');
  put(h, 263, 2, '00');
  put(h, 345, 155, prefix);
  let sum = 0;
  for (const b of h) sum += b;
  put(h, 148, 8, sum.toString(8).padStart(6, '0') + '\0 ');
  return h;
}

/** Uncompressed tar of the files (sorted, no timestamps). */
export function createTar(files: readonly ProjectFile[]): Uint8Array {
  const sorted = [...files].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  let total = 1024;
  for (const f of sorted) total += 512 + Math.ceil(f.data.length / 512) * 512;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const f of sorted) {
    const executable = f.path.endsWith('.sh');
    out.set(header(f.path, f.data.length, executable ? 0o755 : 0o644), offset);
    offset += 512;
    out.set(f.data, offset);
    offset += Math.ceil(f.data.length / 512) * 512;
  }
  return out;
}

export async function gzip(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new CompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/**
 * Files chosen with <input webkitdirectory>, relative to the chosen folder,
 * with the excluded ones left out.
 */
export function selectFiles(list: ArrayLike<File>, extraExcludes: readonly string[] = []): { file: File; path: string }[] {
  const patterns = [...DEFAULT_EXCLUDES, ...extraExcludes];
  const out: { file: File; path: string }[] = [];
  for (const file of Array.from(list)) {
    const rel = (file.webkitRelativePath || file.name).split('/').slice(1).join('/') || file.name;
    if (!isExcluded(rel, patterns)) out.push({ file, path: rel });
  }
  return out.sort((a, b) => (a.path < b.path ? -1 : 1));
}

export async function packFiles(selected: { file: File; path: string }[]): Promise<{ archive: Uint8Array; bytes: number }> {
  const bytes = selected.reduce((sum, s) => sum + s.file.size, 0);
  if (bytes > MAX_PROJECT_BYTES) {
    throw new Error('The folder holds more than 500 MB. Upload large data as a dataset instead.');
  }
  const files = await Promise.all(
    selected.map(async (s) => ({ path: s.path, data: new Uint8Array(await s.file.arrayBuffer()) })),
  );
  return { archive: await gzip(createTar(files)), bytes };
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
