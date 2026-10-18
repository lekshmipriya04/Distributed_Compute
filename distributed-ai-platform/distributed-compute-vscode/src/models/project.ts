// Projects: run a whole folder on the workers. This module is pure (no VS
// Code): which files go in, packing them into a .tar.gz the master accepts,
// reading back the outputs archive, and the `project` block of
// distributed.json.

import { createHash } from 'node:crypto';
import { gunzipSync, gzipSync } from 'node:zlib';

/** Never sent: version control, caches, local environments, earlier results. */
export const DEFAULT_EXCLUDES: readonly string[] = [
  '.git/',
  '.hg/',
  '.svn/',
  'node_modules/',
  '__pycache__/',
  '.venv/',
  'venv/',
  '.mypy_cache/',
  '.pytest_cache/',
  '.ruff_cache/',
  '.ipynb_checkpoints/',
  '.idea/',
  '.vscode/',
  'distributed-results/',
  'outputs/',
  '*.pyc',
  '.DS_Store',
];

/** The master refuses archives over 200 MB; stop well before reading that much. */
export const MAX_PROJECT_BYTES = 500 * 1024 * 1024;
export const MAX_PROJECT_FILES = 50_000;

export interface ProjectBlock {
  /** Shell command run inside the unpacked folder, e.g. "python train.py". */
  command: string;
  /** pip requirements file inside the project. */
  requirements?: string;
  /** Folders (or files) brought back after each task. Default ["outputs"]. */
  outputs?: string[];
  /** Extra patterns to leave out, like "data/raw/" or "*.ckpt". */
  exclude?: string[];
  /** Datasets copied into the project before the command runs. */
  datasets?: { dataset: string; path?: string }[];
}

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

/**
 * gitignore-style matching, simplified: "dir/" leaves out that folder at any
 * depth, a pattern without "/" matches a file or folder name at any depth,
 * and a pattern with "/" matches from the project root.
 */
export function isExcluded(relPath: string, patterns: readonly string[]): boolean {
  const path = relPath.replace(/\\/g, '/');
  const parts = path.split('/');
  for (const raw of patterns) {
    const pattern = raw.trim().replace(/^\.\//, '');
    if (!pattern || pattern.startsWith('#')) {
      continue;
    }
    const dirOnly = pattern.endsWith('/');
    const body = dirOnly ? pattern.slice(0, -1) : pattern;
    const re = globToRegExp(body.replace(/^\//, ''));
    if (body.includes('/')) {
      // Anchored at the root: the path itself or one of its parent folders.
      for (let i = 1; i <= parts.length; i++) {
        if ((dirOnly ? i < parts.length : true) && re.test(parts.slice(0, i).join('/'))) {
          return true;
        }
      }
    } else {
      const names = dirOnly ? parts.slice(0, -1) : parts;
      if (names.some((name) => re.test(name))) {
        return true;
      }
    }
  }
  return false;
}

/** A likely command for a folder, from the files it has. */
export function guessCommand(files: readonly string[]): string {
  const top = new Set(files.filter((f) => !f.includes('/')));
  for (const name of ['train.py', 'main.py', 'run.py', 'app.py']) {
    if (top.has(name)) {
      return `python3 ${name}`;
    }
  }
  if (top.has('run.sh')) {
    return 'sh run.sh';
  }
  if (top.has('Makefile')) {
    return 'make';
  }
  const py = [...top].find((f) => f.endsWith('.py'));
  return py ? `python3 ${py}` : '';
}

export function sha256Hex(data: Uint8Array): string {
  return createHash('sha256').update(data).digest('hex');
}

export interface TarEntry {
  path: string;
  data: Uint8Array;
  /** Unix mode; executable files keep 0o755. */
  mode?: number;
}

function writeString(buf: Buffer, offset: number, length: number, value: string): void {
  buf.write(value, offset, Math.min(length, Buffer.byteLength(value)), 'utf8');
}

function writeOctal(buf: Buffer, offset: number, length: number, value: number): void {
  writeString(buf, offset, length, value.toString(8).padStart(length - 1, '0') + '\0');
}

function splitUstarName(path: string): [string, string] {
  if (Buffer.byteLength(path) <= 100) {
    return [path, ''];
  }
  // ustar keeps long paths as prefix (<= 155 bytes) + "/" + name (<= 100).
  for (let i = path.lastIndexOf('/'); i > 0; i = path.lastIndexOf('/', i - 1)) {
    const prefix = path.slice(0, i);
    const name = path.slice(i + 1);
    if (Buffer.byteLength(prefix) <= 155 && Buffer.byteLength(name) <= 100) {
      return [name, prefix];
    }
  }
  throw new Error(`The path is too long to pack: ${path}`);
}

function tarHeader(path: string, size: number, mode: number): Buffer {
  const header = Buffer.alloc(512);
  const [name, prefix] = splitUstarName(path);
  writeString(header, 0, 100, name);
  writeOctal(header, 100, 8, mode);
  writeOctal(header, 108, 8, 0); // uid
  writeOctal(header, 116, 8, 0); // gid
  writeOctal(header, 124, 12, size);
  writeOctal(header, 136, 12, 0); // mtime 0: the same files always give the same archive
  header.fill(' ', 148, 156); // checksum is computed over spaces
  header[156] = '0'.charCodeAt(0); // regular file
  writeString(header, 257, 6, 'ustar\0');
  writeString(header, 263, 2, '00');
  writeString(header, 345, 155, prefix);
  let sum = 0;
  for (const byte of header) {
    sum += byte;
  }
  writeString(header, 148, 8, sum.toString(8).padStart(6, '0') + '\0 ');
  return header;
}

/**
 * Pack files into a .tar.gz. Entries are sorted and carry no timestamps, so
 * an unchanged project always has the same hash and is never uploaded twice.
 */
export function createTarGz(entries: readonly TarEntry[]): Uint8Array {
  const chunks: Buffer[] = [];
  const sorted = [...entries].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  for (const entry of sorted) {
    const data = Buffer.from(entry.data);
    chunks.push(tarHeader(entry.path, data.length, entry.mode ?? 0o644), data);
    const pad = (512 - (data.length % 512)) % 512;
    if (pad) {
      chunks.push(Buffer.alloc(pad));
    }
  }
  chunks.push(Buffer.alloc(1024));
  return new Uint8Array(gzipSync(Buffer.concat(chunks), { level: 6 }));
}

function readString(buf: Buffer, offset: number, length: number): string {
  const raw = buf.subarray(offset, offset + length);
  const end = raw.indexOf(0);
  return raw.subarray(0, end === -1 ? raw.length : end).toString('utf8');
}

/** True when a path from an archive stays inside the folder it is unpacked in. */
export function isSafeArchivePath(path: string): boolean {
  if (!path || path.startsWith('/') || path.startsWith('\\') || /^[A-Za-z]:/.test(path)) {
    return false;
  }
  return !path.replace(/\\/g, '/').split('/').some((p) => p === '..');
}

/** Regular files of a .tar.gz (ustar, as Python's tarfile writes it). */
export function readTarGz(archive: Uint8Array): TarEntry[] {
  const buf = gunzipSync(Buffer.from(archive));
  const out: TarEntry[] = [];
  let offset = 0;
  let longName: string | undefined;
  while (offset + 512 <= buf.length) {
    const header = buf.subarray(offset, offset + 512);
    if (header.every((b) => b === 0)) {
      break;
    }
    const size = parseInt(readString(header, 124, 12).trim() || '0', 8);
    const type = String.fromCharCode(header[156] || 48);
    const prefix = readString(header, 345, 155);
    let path = longName ?? (prefix ? `${prefix}/${readString(header, 0, 100)}` : readString(header, 0, 100));
    longName = undefined;
    const body = buf.subarray(offset + 512, offset + 512 + size);
    offset += 512 + Math.ceil(size / 512) * 512;
    if (type === 'L') {
      longName = body.toString('utf8').replace(/\0+$/, '');
      continue;
    }
    if (type === 'x') {
      const match = /\d+ path=([^\n]*)\n/.exec(body.toString('utf8'));
      if (match) {
        longName = match[1];
      }
      continue;
    }
    if (type !== '0' && type !== '\0') {
      continue; // folders, links and devices are skipped
    }
    path = path.replace(/^\.\//, '');
    if (!isSafeArchivePath(path)) {
      throw new Error(`Unsafe path in the outputs archive: ${path}`);
    }
    out.push({ path, data: new Uint8Array(body), mode: parseInt(readString(header, 100, 8).trim() || '644', 8) });
  }
  return out;
}

const SAFE_REL = /^[A-Za-z0-9_.@+=,-]+(\/[A-Za-z0-9_.@+=,-]+)*\/?$/;

function isProjectPath(p: unknown): p is string {
  return typeof p === 'string' && SAFE_REL.test(p.replace(/^\.\//, '')) && !p.split('/').includes('..');
}

export function validateProjectBlock(value: unknown): string[] {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return ['"project" must be an object.'];
  }
  const p = value as Record<string, unknown>;
  const errors: string[] = [];
  if (typeof p.command !== 'string' || p.command.trim() === '') {
    errors.push('"project.command" is the command to run in the folder, such as "python train.py".');
  }
  if (p.requirements !== undefined && !isProjectPath(p.requirements)) {
    errors.push('"project.requirements" must be a path inside the project, such as "requirements.txt".');
  }
  if (p.outputs !== undefined && (!Array.isArray(p.outputs) || !p.outputs.every(isProjectPath))) {
    errors.push('"project.outputs" must be a list of folders inside the project, such as ["outputs"].');
  }
  if (p.exclude !== undefined && (!Array.isArray(p.exclude) || p.exclude.some((e) => typeof e !== 'string'))) {
    errors.push('"project.exclude" must be a list of patterns, such as ["data/raw/", "*.ckpt"].');
  }
  if (p.datasets !== undefined) {
    const ok =
      Array.isArray(p.datasets) &&
      p.datasets.every(
        (d) =>
          d !== null &&
          typeof d === 'object' &&
          typeof (d as Record<string, unknown>).dataset === 'string' &&
          ((d as Record<string, unknown>).path === undefined || isProjectPath((d as Record<string, unknown>).path)),
      );
    if (!ok) {
      errors.push('"project.datasets" must be a list of { "dataset": "<id>", "path": "data/train.csv" }.');
    }
  }
  return errors;
}

/** "1.2 MB" */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}
