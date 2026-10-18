import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Code2, Play, Server, Cpu, Square, History, RefreshCw, Loader2, RotateCcw, FolderOpen, Download } from 'lucide-react';
import { useToast } from '../context/ToastContext';
import { Card, SectionTitle, PrimaryButton, SecondaryButton, StatusBadge, NumberField } from '../components/common/Primitives';
import { ApiError, errorMessage } from '../api/client';
import { ExecutionPolicySelect } from '../components/common/ExecutionPolicySelect';
import { SmartLogViewer } from '../components/common/SmartLogViewer';
import type { ExecutionPolicy } from '../api/ssh';
import {
  sshApi,
  executionPolicyLabel,
  shortVersion,
  TERMINAL_BATCH_STATUSES,
  type BatchCreateRequest,
  type BatchResponse,
  type BatchTasksResponse,
  type NodeResponse,
  type RuntimeInfo,
} from '../api/ssh';
import { formatBytes, guessCommand, packFiles, selectFiles } from '../api/project';

const FILENAME_RE = /^[A-Za-z0-9._-]{1,128}$/;
const INPUT_CLS =
  'w-full border border-slate-300 rounded-lg px-3 py-2 bg-white outline-hidden focus:ring-2 focus:ring-blue-500 text-sm';

const isTerminal = (status: string) => TERMINAL_BATCH_STATUSES.includes(status);

export function TaskExecutorPage() {
  const { showSuccess, showError } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  const [nodes, setNodes] = useState<NodeResponse[]>([]);
  const [runtimes, setRuntimes] = useState<RuntimeInfo[]>([]);
  const [runtimesError, setRuntimesError] = useState<string | null>(null);
  const [runtimeId, setRuntimeId] = useState<string>('');
  const [code, setCode] = useState('');
  // The example last loaded into the editor; switching language only replaces
  // the code if the editor still holds exactly this text (or is empty).
  const lastExampleRef = useRef<string>('');
  const [customFilename, setCustomFilename] = useState('main.zig');
  const [customCommand, setCustomCommand] = useState('zig run {file}');

  const [selected, setSelected] = useState<string[]>([]);
  const [inputsText, setInputsText] = useState('alpha\nbeta\ngamma\ndelta');
  const [cpuPerTask, setCpuPerTask] = useState(1);
  const [executionPolicy, setExecutionPolicy] = useState<ExecutionPolicy>('safe_background');
  const [timeoutText, setTimeoutText] = useState('');

  // Project mode: run a whole folder (its modules, configs and requirements).
  const [mode, setMode] = useState<'code' | 'project'>('code');
  const [projectFiles, setProjectFiles] = useState<{ file: File; path: string }[]>([]);
  const [folderName, setFolderName] = useState('');
  const [projectCommand, setProjectCommand] = useState('');
  const [requirements, setRequirements] = useState('');
  const [outputsText, setOutputsText] = useState('outputs');
  const isProject = mode === 'project';

  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [batch, setBatch] = useState<BatchTasksResponse | null>(null);
  const [recent, setRecent] = useState<BatchResponse[]>([]);
  const batchId = searchParams.get('batch');

  const loadNodes = async () => {
    try {
      setNodes((await sshApi.listNodes()).items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return;
      showError(errorMessage(e, 'Failed to load workers'));
    }
  };

  const loadRuntimes = async () => {
    try {
      const res = await sshApi.listRuntimes();
      setRuntimes(res.items);
      setRuntimesError(null);
      return res.items;
    } catch (e) {
      setRuntimesError(errorMessage(e, 'Failed to load languages'));
      return [];
    }
  };

  const loadRecent = async () => {
    try {
      setRecent((await sshApi.listBatches()).items);
    } catch {
      /* list is a convenience */
    }
  };

  useEffect(() => {
    loadNodes();
    loadRecent();
    loadRuntimes().then((items) => {
      if (!items.length) return;
      const initial =
        items.find((r) => r.id === 'python' && r.available_node_ids.length > 0) ??
        items.find((r) => r.id !== 'custom' && r.available_node_ids.length > 0) ??
        items.find((r) => r.id === 'python') ??
        items[0];
      setRuntimeId(initial.id);
      setCode(initial.example_code);
      lastExampleRef.current = initial.example_code;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runtime = runtimes.find((r) => r.id === runtimeId) ?? null;
  const isCustom = runtimeId === 'custom';
  const onlineNodes = useMemo(() => nodes.filter((n) => n.status === 'online'), [nodes]);
  const nodeName = (id?: string | null) => (id ? nodes.find((n) => n.id === id)?.name ?? `${id.slice(0, 8)}…` : '—');

  // Workers that can run the chosen language right now. `custom` relies on the
  // user's own toolchain, so any online worker qualifies when the server does
  // not narrow it down.
  const eligibleIds = useMemo(() => {
    if (isProject) {
      // Projects need python3 for the helper, on Linux or macOS.
      const py = new Set((runtimes.find((r) => r.id === 'python')?.available_node_ids ?? []).map(String));
      return new Set(onlineNodes.filter((n) => py.has(n.id) && n.os_type !== 'windows').map((n) => n.id));
    }
    if (!runtime) return new Set<string>();
    if (runtime.id === 'custom' && runtime.available_node_ids.length === 0) {
      return new Set(onlineNodes.map((n) => n.id));
    }
    return new Set(runtime.available_node_ids.map(String));
  }, [runtime, onlineNodes, isProject, runtimes]);

  // Drop selections that cannot run the newly chosen language.
  useEffect(() => {
    setSelected((cur) => {
      const kept = cur.filter((id) => eligibleIds.has(id));
      return kept.length === cur.length ? cur : kept;
    });
  }, [eligibleIds]);

  const runtimeAvailable = (r: RuntimeInfo) => r.id === 'custom' || r.available_node_ids.length > 0;

  const changeRuntime = (id: string) => {
    const next = runtimes.find((r) => r.id === id);
    if (!next) return;
    setRuntimeId(id);
    if (code === lastExampleRef.current || !code.trim()) {
      setCode(next.example_code);
    }
    lastExampleRef.current = next.example_code;
  };

  const toggle = (id: string) =>
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  // --- validation --------------------------------------------------------
  const filenameError =
    isCustom && !FILENAME_RE.test(customFilename) ? 'Letters, digits, ".", "_" and "-" only (max 128), e.g. main.zig' : null;
  const commandError = isCustom
    ? !customCommand.includes('{file}')
      ? 'Must contain {file}, which is replaced by the source file path'
      : customCommand.length > 2048
        ? 'At most 2048 characters'
        : null
    : null;
  const timeoutValue = timeoutText.trim() === '' ? null : Number(timeoutText);
  const timeoutError =
    timeoutValue !== null && (!Number.isInteger(timeoutValue) || timeoutValue < 1) ? 'Whole seconds, at least 1' : null;
  const targetIds = selected.length ? selected : [...eligibleIds];

  const blocker = isProject
    ? projectFiles.length === 0
      ? 'Choose the project folder.'
      : !projectCommand.trim()
        ? 'Enter the command to run in the folder.'
        : timeoutError
          ? 'Fix the highlighted fields.'
          : targetIds.length === 0
            ? 'No online Linux or macOS worker has python3.'
            : null
    : !runtime
    ? 'Choose a language.'
    : !code.trim()
      ? 'Code is required.'
      : filenameError || commandError || timeoutError
        ? 'Fix the highlighted fields.'
        : targetIds.length === 0
          ? `No online worker has ${runtime.name}.`
          : null;

  const running = batch !== null && !isTerminal(batch.batch.status);
  const unfinished = batch ? batch.batch.failed + (batch.batch.cancelled ?? 0) : 0;

  const chooseFolder = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    const picked = selectFiles(list);
    setProjectFiles(picked);
    setFolderName(list[0].webkitRelativePath.split('/')[0] || 'folder');
    const paths = picked.map((p) => p.path);
    setProjectCommand((cur) => cur || guessCommand(paths));
    setRequirements(paths.includes('requirements.txt') ? 'requirements.txt' : '');
  };

  const handleSubmitProject = async () => {
    const inputs = inputsText.split('\n').map((s) => s.trim()).filter(Boolean);
    setSubmitting(true);
    try {
      const { archive } = await packFiles(projectFiles);
      const bundle = await sshApi.uploadBundle(archive);
      const outputs = outputsText.split(',').map((o) => o.trim()).filter(Boolean);
      const created = await sshApi.createBatch({
        code: '',
        inputs: inputs.length ? inputs : [''],
        node_ids: targetIds,
        runtime: 'project',
        cpu_per_task: cpuPerTask,
        timeout_seconds: timeoutValue,
        execution_policy: executionPolicy,
        project: {
          bundle_id: bundle.id,
          command: projectCommand.trim(),
          requirements: requirements.trim() || null,
          outputs: outputs.length ? outputs : ['outputs'],
        },
      });
      setBatch({ batch: created, tasks: [] });
      setSearchParams({ batch: created.id });
      showSuccess(`Project started (${created.total} task${created.total === 1 ? '' : 's'})`);
      loadRecent();
    } catch (e) {
      showError(errorMessage(e, 'Could not start the project'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmit = async () => {
    if (blocker) return;
    if (isProject) return handleSubmitProject();
    if (!runtime) return;
    const inputs = inputsText.split('\n').map((s) => s.trim()).filter(Boolean);
    const body: BatchCreateRequest = {
      code,
      inputs: inputs.length ? inputs : [''],
      node_ids: targetIds,
      runtime: runtime.id,
      custom_command: isCustom ? customCommand : null,
      custom_filename: isCustom ? customFilename : null,
      cpu_per_task: cpuPerTask,
      timeout_seconds: timeoutValue,
      execution_policy: executionPolicy,
    };
    setSubmitting(true);
    try {
      const created = await sshApi.createBatch(body);
      setBatch({ batch: created, tasks: [] });
      setSearchParams({ batch: created.id });
      showSuccess(`Batch submitted (${created.total} tasks)`);
      loadRecent();
    } catch (e) {
      showError(errorMessage(e, 'Submit failed'));
    } finally {
      setSubmitting(false);
    }
  };

  const [retrying, setRetrying] = useState(false);
  const handleRetry = async (failedOnly: boolean) => {
    if (!batch) return;
    setRetrying(true);
    try {
      const created = await sshApi.retryBatch(batch.batch.id, failedOnly);
      setBatch({ batch: created, tasks: [] });
      setSearchParams({ batch: created.id });
      showSuccess(`Started again (${created.total} task${created.total === 1 ? '' : 's'})`);
      loadRecent();
    } catch (e) {
      showError(errorMessage(e, 'Retry failed'));
    } finally {
      setRetrying(false);
    }
  };

  const handleCancel = async () => {
    if (!batch) return;
    setCancelling(true);
    try {
      const updated = await sshApi.cancelBatch(batch.batch.id);
      setBatch((cur) => (cur ? { ...cur, batch: updated } : cur));
    } catch (e) {
      showError(errorMessage(e, 'Cancel failed'));
    } finally {
      setCancelling(false);
    }
  };

  // Poll the open batch until it reaches a terminal status.
  useEffect(() => {
    if (!batchId) {
      setBatch(null);
      return;
    }
    let active = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const res = await sshApi.getBatchTasks(batchId);
        if (!active) return;
        setBatch(res);
        if (isTerminal(res.batch.status)) {
          loadRecent();
          return;
        }
      } catch (e) {
        if (!active) return;
        if (e instanceof ApiError && e.status === 404) {
          showError('Batch not found');
          setSearchParams({});
          return;
        }
        /* transient; keep polling */
      }
      timer = window.setTimeout(tick, 2000);
    };
    tick();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId]);

  return (
    <div className="w-full max-w-4xl mx-auto space-y-6">
      {/* Language ---------------------------------------------------------- */}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
          <div className="flex items-center gap-2">
            {isProject ? <FolderOpen size={18} className="text-blue-600" /> : <Code2 size={18} className="text-blue-600" />}
            <SectionTitle>{isProject ? 'Project folder' : 'Language'}</SectionTitle>
          </div>
          <div className="inline-flex rounded-lg border border-slate-300 p-0.5 text-sm" role="radiogroup" aria-label="What to run">
            {(['code', 'project'] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                onClick={() => setMode(m)}
                className={`px-3 py-1 rounded-md focus:outline-hidden focus:ring-2 focus:ring-blue-500 ${
                  mode === m ? 'bg-blue-600 text-white' : 'text-slate-700 hover:text-blue-700'
                }`}
              >
                {m === 'code' ? 'Single file' : 'Project folder'}
              </button>
            ))}
          </div>
        </div>
        <p className="text-sm text-slate-500 mb-4">
          {isProject
            ? 'Send a whole folder (your modules, configs and requirements.txt). Each worker builds the Python environment, runs your command in the folder, and sends back what it writes to the outputs folder.'
            : 'Any language already present on your workers can be used; nothing is installed. Each task runs in a throwaway sandbox that is deleted afterwards.'}
        </p>
        {isProject ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="sm:col-span-2">
              <label className="inline-flex items-center gap-2 cursor-pointer rounded-lg border border-dashed border-slate-300 px-4 py-3 text-sm text-slate-700 hover:border-blue-400 focus-within:ring-2 focus-within:ring-blue-500">
                <FolderOpen size={16} className="text-blue-600" />
                {projectFiles.length ? 'Choose another folder' : 'Choose the project folder'}
                <input
                  type="file"
                  className="sr-only"
                  aria-label="Project folder"
                  data-testid="project-folder"
                  // @ts-expect-error: non-standard but supported by every current browser
                  webkitdirectory=""
                  multiple
                  onChange={(e) => chooseFolder(e.target.files)}
                />
              </label>
              {projectFiles.length > 0 && (
                <p className="text-xs text-slate-500 mt-2" data-testid="project-summary">
                  <span className="font-medium text-slate-700">{folderName}</span> · {projectFiles.length} file
                  {projectFiles.length === 1 ? '' : 's'} · {formatBytes(projectFiles.reduce((n, f) => n + f.file.size, 0))}
                  {' '}(.git, caches, virtual environments and outputs/ are left out)
                </p>
              )}
            </div>
            <label className="text-sm sm:col-span-2">
              <span className="text-xs text-slate-500 font-medium block mb-1">Command, run inside the folder</span>
              <input
                className={`${INPUT_CLS} font-mono`}
                value={projectCommand}
                onChange={(e) => setProjectCommand(e.target.value)}
                placeholder="python3 train.py --epochs 3"
              />
            </label>
            <label className="text-sm">
              <span className="text-xs text-slate-500 font-medium block mb-1">Requirements file (optional)</span>
              <input
                className={`${INPUT_CLS} font-mono`}
                value={requirements}
                onChange={(e) => setRequirements(e.target.value)}
                placeholder="requirements.txt"
              />
            </label>
            <label className="text-sm">
              <span className="text-xs text-slate-500 font-medium block mb-1">Folders to bring back</span>
              <input
                className={`${INPUT_CLS} font-mono`}
                value={outputsText}
                onChange={(e) => setOutputsText(e.target.value)}
                placeholder="outputs"
              />
            </label>
          </div>
        ) : runtimesError ? (
          <div className="flex items-center gap-3 text-sm text-rose-700">
            {runtimesError}
            <SecondaryButton onClick={loadRuntimes}>Retry</SecondaryButton>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="sm:col-span-2">
              <span className="text-xs text-slate-500 font-medium block mb-2">Language</span>
              {runtimes.length === 0 ? (
                <span className="text-sm text-slate-500">Loading…</span>
              ) : (
                <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Language">
                  {[...runtimes]
                    .sort((a, b) => Number(runtimeAvailable(b)) - Number(runtimeAvailable(a)))
                    .map((r) => {
                      const available = runtimeAvailable(r);
                      const active = r.id === runtimeId;
                      return (
                        <button
                          key={r.id}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          disabled={!available}
                          onClick={() => changeRuntime(r.id)}
                          title={available ? `${r.name}${r.id === 'custom' ? '' : ` · ${r.available_node_ids.length} worker(s)`}` : `${r.name} was not found on your workers`}
                          className={`rounded-full border px-3 py-1 text-sm transition-colors focus:outline-hidden focus:ring-2 focus:ring-blue-500 ${
                            active
                              ? 'border-blue-600 bg-blue-600 text-white shadow-xs'
                              : available
                                ? 'border-slate-300 bg-white text-slate-700 hover:border-blue-400 hover:text-blue-700'
                                : 'border-dashed border-slate-200 bg-slate-50 text-slate-500 cursor-not-allowed'
                          }`}
                        >
                          {r.name}
                        </button>
                      );
                    })}
                </div>
              )}
            </div>
            {runtime && (
              <div className="text-xs text-slate-500 sm:col-span-2">
                {runtime.kind === 'compiled' && 'Compiled inside the sandbox before running. '}
                {runtime.kind === 'custom' && 'Runs your own command with the same confinement as built-in languages. '}
                {!runtime.windows_supported && 'Not available on Windows workers. '}
                {runtime.id !== 'custom' && `Available on ${runtime.available_node_ids.length} online worker(s).`}
              </div>
            )}
            {isCustom && (
              <>
                <label className="text-sm">
                  <span className="text-xs text-slate-500 font-medium block mb-1">Filename</span>
                  <input
                    className={`${INPUT_CLS} font-mono ${filenameError ? 'border-rose-400' : ''}`}
                    value={customFilename}
                    onChange={(e) => setCustomFilename(e.target.value)}
                    placeholder="main.zig"
                  />
                  {filenameError && <span className="text-xs text-rose-600 mt-1 block">{filenameError}</span>}
                </label>
                <label className="text-sm">
                  <span className="text-xs text-slate-500 font-medium block mb-1">Run command</span>
                  <input
                    className={`${INPUT_CLS} font-mono ${commandError ? 'border-rose-400' : ''}`}
                    value={customCommand}
                    onChange={(e) => setCustomCommand(e.target.value)}
                    placeholder="zig run {file}"
                  />
                  {commandError && <span className="text-xs text-rose-600 mt-1 block">{commandError}</span>}
                </label>
              </>
            )}
          </div>
        )}
      </Card>

      {/* Workers ------------------------------------------------------------ */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Server size={18} className="text-blue-600" />
            <SectionTitle>Workers</SectionTitle>
          </div>
          <span className="text-sm text-slate-500">
            {selected.length ? `${selected.length} selected` : `all ${eligibleIds.size} eligible`} · {onlineNodes.length} online
          </span>
        </div>
        {nodes.length === 0 ? (
          <p className="text-sm text-slate-500">
            No workers yet. Add one on the <span className="font-medium">SSH Connection</span> page.
          </p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {nodes.map((n) => {
              const eligible = eligibleIds.has(n.id);
              const active = selected.includes(n.id);
              const version = runtime ? n.runtimes?.[runtime.id] : undefined;
              const reason =
                n.status !== 'online'
                  ? n.status
                  : isProject
                    ? eligible
                      ? `python ${shortVersion('Python', n.runtimes?.python ?? '')}${n.env_cache_gb ? ` · ${n.env_cache_gb} GB env cache` : ''}`.trim()
                      : n.os_type === 'windows'
                        ? 'Windows: not yet'
                        : 'no python3'
                  : isCustom
                    ? 'uses your command'
                    : version !== undefined
                      ? `${runtime?.name ?? ''} ${shortVersion(runtime?.name ?? '', version)}`.trim()
                      : `no ${runtime?.name ?? 'runtime'}`;
              return (
                <button
                  key={n.id}
                  type="button"
                  onClick={() => eligible && toggle(n.id)}
                  disabled={!eligible}
                  className={`text-left px-3.5 py-2.5 rounded-lg border text-sm transition-all ${
                    active
                      ? 'bg-blue-50 border-blue-500 text-blue-800'
                      : eligible
                        ? 'bg-white border-slate-200 text-slate-700 hover:border-slate-300'
                        : 'bg-slate-50 border-slate-200 text-slate-500 cursor-not-allowed'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{n.name}</span>
                    <StatusBadge status={n.status} />
                  </div>
                  <div className="text-xs mt-0.5 flex justify-between gap-2">
                    <span className="text-slate-500">
                      {n.host}:{n.port} · {n.allocated_cpu} cores
                    </span>
                    <span className={eligible ? 'text-emerald-700' : 'text-slate-500'}>{reason}</span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
        <p className="text-xs text-slate-500 mt-3">
          {isProject
            ? 'Projects run on Linux and macOS workers with python3. Leave all unselected to use every eligible worker.'
            : 'Only workers that have the chosen language can be selected. Leave all unselected to use every eligible worker.'}
        </p>
      </Card>

      {/* Code + inputs ------------------------------------------------------ */}
      <Card>
        <SectionTitle>{isProject ? 'Inputs' : 'Code & inputs'}</SectionTitle>
        <div className={`grid grid-cols-1 gap-4 ${isProject ? '' : 'md:grid-cols-2'}`}>
          <div className={isProject ? 'hidden' : ''}>
            <label htmlFor="exec-code" className="block text-sm font-medium mb-1 text-slate-700">
              Code {runtime && !isCustom ? <span className="text-slate-500 font-normal font-mono">({runtime.filename})</span> : null}
            </label>
            <textarea
              id="exec-code"
              className="w-full border border-slate-300 rounded-lg p-3 bg-slate-50 font-mono text-sm h-64 outline-hidden focus:ring-2 focus:ring-blue-500 shadow-inner"
              value={code}
              spellCheck={false}
              onChange={(e) => setCode(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="exec-inputs" className="block text-sm font-medium mb-1 text-slate-700">
              Inputs{' '}
              <span className="text-slate-500 font-normal">
                {isProject
                  ? '(one line = one task, on stdin and in $DC_INPUT_FILE; for example one learning rate per line)'
                  : '(one line = one task, delivered on stdin and as input.txt)'}
              </span>
            </label>
            <textarea
              id="exec-inputs"
              className={`w-full border border-slate-300 rounded-lg p-3 font-mono text-sm outline-hidden focus:ring-2 focus:ring-blue-500 ${isProject ? 'h-32' : 'h-64'}`}
              value={inputsText}
              spellCheck={false}
              onChange={(e) => setInputsText(e.target.value)}
            />
          </div>
        </div>
        <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <label className="text-sm">
            <span className="text-xs text-slate-500 font-medium block mb-1">Cores per task</span>
            <NumberField value={cpuPerTask} min={1} max={256} step={1} onChange={(n) => setCpuPerTask(Math.round(n))} className={INPUT_CLS} />
          </label>
          <ExecutionPolicySelect value={executionPolicy} onChange={setExecutionPolicy} className={INPUT_CLS} />
          <label className="text-sm">
            <span className="text-xs text-slate-500 font-medium block mb-1">Timeout (s)</span>
            <input
              className={`${INPUT_CLS} ${timeoutError ? 'border-rose-400' : ''}`}
              inputMode="numeric"
              value={timeoutText}
              onChange={(e) => setTimeoutText(e.target.value.replace(/[^\d]/g, ''))}
              placeholder="default"
            />
            {timeoutError && <span className="text-xs text-rose-600 mt-1 block">{timeoutError}</span>}
          </label>
        </div>
        <div className="mt-5 flex flex-wrap items-center gap-4">
          <PrimaryButton onClick={handleSubmit} disabled={submitting || !!blocker || running}>
            <span className="flex items-center gap-2">
              <Play size={16} /> {submitting ? (isProject ? 'Sending the project…' : 'Submitting…') : isProject ? 'Run project' : 'Run in parallel'}
            </span>
          </PrimaryButton>
          {blocker && <span className="text-sm text-slate-500">{blocker}</span>}
          {!blocker && running && <span className="text-sm text-slate-500">A batch is still running.</span>}
        </div>
      </Card>

      {/* Results ------------------------------------------------------------ */}
      {batch && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
            <div className="flex items-center gap-2">
              {running ? <Loader2 size={18} className="text-blue-600 animate-spin" /> : <Cpu size={18} className="text-blue-600" />}
              <h3 className="font-semibold text-slate-900">
                Batch {batch.batch.id.slice(0, 8)}{' '}
                <span className="text-slate-500 font-normal">
                  · {batch.batch.project ? <span className="font-mono">{batch.batch.project.command}</span> : batch.batch.runtime} ·{' '}
                  {executionPolicyLabel(batch.batch.execution_policy)}
                </span>
              </h3>
            </div>
            <div className="flex items-center gap-3 text-sm">
              <StatusBadge status={batch.batch.status} />
              <span className="text-slate-500" data-testid="batch-summary">
                {batch.batch.completed}/{batch.batch.total} done · {batch.batch.failed} failed
                {batch.batch.cancelled ? ` · ${batch.batch.cancelled} cancelled` : ''}
              </span>
              {!running && unfinished > 0 && unfinished < batch.batch.total && (
                <SecondaryButton onClick={() => handleRetry(true)} disabled={retrying}>
                  <span className="flex items-center gap-1.5">
                    <RotateCcw size={14} /> Retry {unfinished} failed
                  </span>
                </SecondaryButton>
              )}
              {!running && (
                <SecondaryButton onClick={() => handleRetry(false)} disabled={retrying}>
                  <span className="flex items-center gap-1.5">
                    <RotateCcw size={14} /> {retrying ? 'Starting…' : 'Run again'}
                  </span>
                </SecondaryButton>
              )}
              {running && (
                <SecondaryButton onClick={handleCancel} disabled={cancelling || batch.batch.status === 'cancelling'}>
                  <span className="flex items-center gap-1.5">
                    <Square size={14} /> {batch.batch.status === 'cancelling' || cancelling ? 'Cancelling…' : 'Cancel'}
                  </span>
                </SecondaryButton>
              )}
            </div>
          </div>
          {batch.batch.message && <p className="text-sm text-slate-600 mb-3">{batch.batch.message}</p>}
          <div className="space-y-3">
            {batch.tasks.length === 0 && <p className="text-sm text-slate-500 italic">Waiting for tasks…</p>}
            {batch.tasks.map((t) => (
              <div key={t.id} className="border border-slate-200 rounded-lg overflow-hidden">
                <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 bg-slate-50">
                  <span className="text-sm font-medium text-slate-700">
                    Task {t.seq + 1}
                    {t.attempts > 1 ? ` (attempt ${t.attempts})` : ''}
                    <span className="text-slate-500 font-normal"> · {nodeName(t.node_id)}</span>
                  </span>
                  <span className="flex items-center gap-2">
                    {(t.output_files ?? 0) > 0 && (
                      <button
                        type="button"
                        onClick={() =>
                          sshApi.downloadTaskOutputs(batch.batch.id, t.id, t.seq).catch((e) => showError(errorMessage(e, 'Download failed')))
                        }
                        className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700 hover:border-blue-400 hover:text-blue-700"
                        title="Download what this task wrote to its output folders (.tar.gz)"
                      >
                        <Download size={12} /> Outputs ({t.output_files} file{t.output_files === 1 ? '' : 's'}
                        {t.output_bytes ? `, ${formatBytes(t.output_bytes)}` : ''})
                      </button>
                    )}
                    <StatusBadge status={t.status} />
                  </span>
                </div>
                <SmartLogViewer
                  stdout={t.stdout}
                  stderr={t.stderr}
                  exitCode={t.exit_code}
                  status={t.status}
                  durationMs={t.duration_ms}
                  label={batch.batch.project ? 'project' : batch.batch.runtime}
                />
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Recent batches ----------------------------------------------------- */}
      <Card>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <History size={18} className="text-blue-600" />
            <h3 className="font-semibold text-slate-900">Recent batches</h3>
          </div>
          <button onClick={loadRecent} className="text-slate-500 hover:text-slate-700" title="Reload">
            <RefreshCw size={15} />
          </button>
        </div>
        {recent.length === 0 ? (
          <p className="text-sm text-slate-500 italic">No batches yet.</p>
        ) : (
          <div className="space-y-1.5">
            {recent.map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => setSearchParams({ batch: b.id })}
                className={`w-full flex flex-wrap items-center gap-3 text-left rounded-lg border px-3 py-2 text-sm transition-colors ${
                  b.id === batchId ? 'border-blue-500 bg-blue-50' : 'border-slate-200 hover:border-slate-300'
                }`}
              >
                <span className="font-mono text-slate-700">{b.id.slice(0, 8)}</span>
                <span className="text-slate-500 truncate max-w-xs">{b.project ? b.project.command : b.runtime}</span>
                <span className="text-slate-500">
                  {b.completed}/{b.total} done{b.failed ? ` · ${b.failed} failed` : ''}
                </span>
                <span className="text-xs text-slate-500">{new Date(b.created_at).toLocaleString()}</span>
                <span className="ml-auto">
                  <StatusBadge status={b.status} />
                </span>
              </button>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
