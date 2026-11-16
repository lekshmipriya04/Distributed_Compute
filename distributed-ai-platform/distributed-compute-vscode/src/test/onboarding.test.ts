import { describe, expect, it } from 'vitest';
import {
  detectionSummary,
  formatTaskLog,
  isLoopbackHost,
  parseInputLines,
  parseInputList,
  parsePort,
  resultFileName,
  sliceChoices,
  coresError,
  memoryError,
  validateHost,
  type ExecutionTasks,
  type NodeCapabilities,
} from '../models/onboarding';

describe('onboarding helpers', () => {
  it('validates hostnames and IP addresses', () => {
    expect(validateHost('')).toBe('Enter the worker’s hostname or IP address');
    expect(validateHost('   ')).toBe('Enter the worker’s hostname or IP address');
    expect(validateHost('192.168.1.50')).toBeUndefined();
    expect(validateHost('host.docker.internal')).toBeUndefined();
    expect(validateHost('worker-node-1')).toBeUndefined();
    expect(validateHost('bad host with spaces')).toBeDefined();
    expect(validateHost('rm -rf /;')).toBeDefined();
  });

  it('identifies loopback hosts correctly', () => {
    expect(isLoopbackHost('localhost')).toBe(true);
    expect(isLoopbackHost('127.0.0.1')).toBe(true);
    expect(isLoopbackHost('::1')).toBe(true);
    expect(isLoopbackHost('192.168.1.10')).toBe(false);
    expect(isLoopbackHost('host.docker.internal')).toBe(false);
  });

  it('parses valid port numbers', () => {
    expect(parsePort('22')).toBe(22);
    expect(parsePort('2222')).toBe(2222);
    expect(parsePort('8090')).toBe(8090);
    expect(parsePort('0')).toBeUndefined();
    expect(parsePort('70000')).toBeUndefined();
    expect(parsePort('abc')).toBeUndefined();
  });

  it('formats detection summaries with GPU and runtimes', () => {
    const caps: NodeCapabilities = {
      detected_cpu: 8,
      detected_memory_gb: 16.0,
      gpu_count: 1,
      gpu_model: 'Apple M2 Pro',
      gpu_runtime: 'torch-mps',
      runtimes: { python: '3.11', node: '20.1' },
    };
    const summary = detectionSummary(caps);
    expect(summary).toContain('8 cores');
    expect(summary).toContain('16 GB RAM');
    expect(summary).toContain('Apple M2 Pro (torch-mps)');
    expect(summary).toContain('python 3.11, node 20.1');

    const noGpuCaps: NodeCapabilities = {
      detected_cpu: 4,
      detected_memory_gb: 8.0,
      gpu_count: 0,
      runtimes: {},
    };
    const summaryNoGpu = detectionSummary(noGpuCaps);
    expect(summaryNoGpu).toContain('no GPU');
    expect(summaryNoGpu).toContain('no languages found');
  });

  it('computes sensible slice choices', () => {
    const choices = sliceChoices({ detected_cpu: 8, detected_memory_gb: 16.0 });
    expect(choices.length).toBeGreaterThanOrEqual(3);
    expect(choices[0].label).toBe('Half the machine');
    expect(choices[0].cpu).toBe(4);
    expect(choices[0].memoryGb).toBe(8);

    const fullChoice = choices.find((c) => c.label === 'Everything');
    expect(fullChoice?.cpu).toBe(8);
    expect(fullChoice?.memoryGb).toBe(16);
  });

  it('accepts any whole core count within the machine', () => {
    for (const ok of ['1', '2', '3', '12', ' 7 ']) expect(coresError(ok, 12)).toBeUndefined();
    for (const bad of ['', '0', '13', '2.5', '-1', 'two']) expect(coresError(bad, 12)).toMatch(/1 to 12/);
    expect(coresError('64')).toBeUndefined();
  });

  it('accepts memory separately, including fractions of a GB', () => {
    for (const ok of ['0.5', '4', '12', '24']) expect(memoryError(ok, 24)).toBeUndefined();
    for (const bad of ['', '0', '-2', '24.5', 'lots']) expect(memoryError(bad, 24)).toMatch(/up to 24/);
  });

  it('parses input lines and lists', () => {
    const lines = parseInputLines('first line\n\nsecond line  \nthird line\n');
    expect(lines).toEqual(['first line', 'second line', 'third line']);

    const list = parseInputList('alpha, beta,  gamma,');
    expect(list).toEqual(['alpha', 'beta', 'gamma']);
  });

  it('formats execution task logs cleanly', () => {
    const tasks: ExecutionTasks = {
      batch: { id: 'batch-1234', runtime: 'python', status: 'completed', total: 2 },
      tasks: [
        {
          id: 'task-1',
          seq: 0,
          node_id: 'node-a',
          status: 'completed',
          exit_code: 0,
          stdout: 'output line 1\noutput line 2',
          attempts: 1,
          duration_ms: 1200,
        },
        {
          id: 'task-2',
          seq: 1,
          node_id: 'node-b',
          status: 'failed',
          exit_code: 1,
          stderr: 'error trace',
          attempts: 1,
          duration_ms: 800,
        },
      ],
    };
    const formatted = formatTaskLog(tasks, { 'node-a': 'worker-1', 'node-b': 'worker-2' });
    expect(formatted).toContain('Execution batch-1234');
    expect(formatted).toContain('Task 1 · completed · exit 0 · 1.2s · worker-1');
    expect(formatted).toContain('output line 1');
    expect(formatted).toContain('Task 2 · failed · exit 1 · 0.8s · worker-2');
    expect(formatted).toContain('error trace');
  });

  it('generates padded result file names', () => {
    expect(resultFileName(0, 10, 'out')).toBe('task-001.out.txt');
    expect(resultFileName(9, 10, 'err')).toBe('task-010.err.txt');
    expect(resultFileName(99, 1000, 'out')).toBe('task-0100.out.txt');
  });
});
