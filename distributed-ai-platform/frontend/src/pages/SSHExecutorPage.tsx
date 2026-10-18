import { useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Boxes,
  Check,
  Copy,
  Cpu,
  Fingerprint,
  KeyRound,
  Loader2,
  PauseCircle,
  Pencil,
  PlayCircle,
  Plus,
  RefreshCw,
  RotateCw,
  Server,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react';
import { useToast } from '../context/ToastContext';
import { Card, ConfirmDialog, NumberField, PrimaryButton, SecondaryButton, StatusBadge } from '../components/common/Primitives';
import { ApiError, errorMessage } from '../api/client';
import {
  shortVersion,
  sshApi,
  type NodeCapabilities,
  type NodeConnectResponse,
  type NodeManualProvisionResponse,
  type NodeProvisionResponse,
  type NodeResponse,
} from '../api/ssh';

const INPUT_CLS =
  'w-full border border-slate-300 rounded-lg p-2 text-sm outline-hidden focus:ring-2 focus:ring-blue-500 bg-white';

type Step = 'form' | 'confirm' | 'manual' | 'detected';
type Notice = { tone: 'error' | 'warning'; title: string; body: string } | null;

/** Memory slider bounds: half-GB steps, never above what was detected. */
function memBounds(detectedGb: number) {
  const max = Math.max(0.5, Math.floor(detectedGb * 2) / 2);
  return { min: Math.min(0.5, max), max };
}

/** Detected GPU count (0 when the node has no usable GPU). */
function gpuCount(caps: NodeCapabilities): number {
  return Math.max(caps.gpu_count || 0, caps.detected_gpu || 0);
}

/** Detected VRAM in GB, 0 when no GPU (or unknown memory). */
function gpuTotalGb(caps: NodeCapabilities): number {
  return gpuCount(caps) > 0 && caps.gpu_memory_gb ? caps.gpu_memory_gb : 0;
}

const fmtGb = (gb: number) => `${Number.isInteger(gb) ? gb : gb.toFixed(1)}GB`;

/** "Slice: 8GB / 24GB VRAM | 4 / 10 cores" (CPU-only: "Slice: 4 / 10 cores"). */
export function sliceSummary(node: NodeResponse): string {
  const cores = `${node.allocated_cpu} / ${node.detected_cpu} cores`;
  const total = gpuTotalGb(node);
  if (total <= 0) return `Slice: ${cores}`;
  return `Slice: ${fmtGb(node.allocated_gpu_memory_gb ?? 0)} / ${fmtGb(total)} VRAM | ${cores}`;
}

const GPU_PRESETS = [
  { label: '25%', frac: 0.25 },
  { label: '33%', frac: 1 / 3 },
  { label: '50%', frac: 0.5 },
];

/** GPU memory slice: presets + custom slider, 0..detected VRAM. */
function GpuSliceControl({
  caps,
  value,
  onChange,
}: {
  caps: NodeCapabilities;
  value: number;
  onChange: (gb: number) => void;
}) {
  const total = gpuTotalGb(caps);
  if (total <= 0) {
    return (
      <div className="text-sm">
        <span className="text-xs text-slate-500 font-medium block mb-1">GPU slice</span>
        <input type="range" disabled min={0} max={1} value={0} className="w-full opacity-40" readOnly />
        <span className="text-xs text-slate-500">no GPU</span>
      </div>
    );
  }
  const round = (gb: number) => Math.min(total, Math.round(gb * 2) / 2);
  const activePreset = GPU_PRESETS.find((p) => round(total * p.frac) === value);
  return (
    <div className="text-sm">
      <span className="text-xs text-slate-500 font-medium block mb-1">
        GPU slice: {fmtGb(value)} / {fmtGb(total)} VRAM
      </span>
      <div className="flex flex-wrap gap-1 mb-1">
        {GPU_PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => onChange(round(total * p.frac))}
            className={`text-xs px-2 py-0.5 rounded-md border ${
              activePreset === p ? 'bg-blue-600 text-white border-blue-600' : 'border-slate-300 text-slate-600 hover:bg-slate-50'
            }`}
          >
            {p.label}
          </button>
        ))}
        <label className="text-xs flex items-center gap-1 text-slate-600">
          Custom
          <input
            type="number"
            min={0}
            max={total}
            step={0.5}
            value={value}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              onChange(Number.isFinite(v) ? Math.max(0, Math.min(total, v)) : 0);
            }}
            className="w-16 border border-slate-300 rounded-sm px-1 py-0.5"
          />
          GB
        </label>
      </div>
      <input
        type="range"
        min={0}
        max={total}
        step={0.5}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-blue-600"
      />
    </div>
  );
}

/** allocated_gpu_* fields for a register/update request. */
function gpuAllocation(caps: NodeCapabilities, gb: number) {
  const total = gpuTotalGb(caps);
  if (total <= 0) return { allocated_gpu_count: 0, allocated_gpu_memory_gb: 0 };
  return { allocated_gpu_count: gb > 0 ? gpuCount(caps) : 0, allocated_gpu_memory_gb: gb };
}

export function SSHExecutorPage() {
  const { showError } = useToast();

  const [nodes, setNodes] = useState<NodeResponse[]>([]);
  const [runtimeNames, setRuntimeNames] = useState<Record<string, string>>({});

  const loadNodes = async () => {
    try {
      const res = await sshApi.listNodes();
      setNodes(res.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return; // not signed in yet
      showError(errorMessage(e, 'Failed to load workers'));
    }
  };

  useEffect(() => {
    loadNodes();
    sshApi
      .listRuntimes()
      .then((r) => setRuntimeNames(Object.fromEntries(r.items.map((rt) => [rt.id, rt.name]))))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const replaceNode = (updated: NodeResponse) =>
    setNodes((cur) => cur.map((n) => (n.id === updated.id ? updated : n)));

  const totalAllocatedCores = nodes.reduce((sum, n) => sum + n.allocated_cpu, 0);

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <OnboardingCard runtimeNames={runtimeNames} onRegistered={loadNodes} />

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 mb-4">
          <div className="flex items-center gap-2">
            <Server size={18} className="text-blue-600" />
            <h2 className="text-lg font-semibold text-slate-900">Workers</h2>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-slate-500">
              {totalAllocatedCores} cores lent across {nodes.length} worker(s)
            </span>
            <button onClick={loadNodes} className="text-slate-500 hover:text-slate-700" title="Reload list">
              <RefreshCw size={15} />
            </button>
          </div>
        </div>
        {nodes.length === 0 ? (
          <p className="text-sm text-slate-500">No workers yet. Add one above.</p>
        ) : (
          <div className="space-y-2">
            {nodes.map((n) => (
              <NodeRow
                key={n.id}
                node={n}
                runtimeNames={runtimeNames}
                onUpdated={replaceNode}
                onDeleted={() => setNodes((cur) => cur.filter((x) => x.id !== n.id))}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Onboarding: host/port/user -> confirm host key -> password -> resources
// ---------------------------------------------------------------------------
function OnboardingCard({
  runtimeNames,
  onRegistered,
}: {
  runtimeNames: Record<string, string>;
  onRegistered: () => void;
}) {
  const { showSuccess, showError } = useToast();

  const [step, setStep] = useState<Step>('form');
  const [form, setForm] = useState({ name: '', host: '', port: 22, username: '' });
  const [connectInfo, setConnectInfo] = useState<NodeConnectResponse | null>(null);
  // Lives only in this component's state and is cleared as soon as the
  // provision request is sent. Never stored anywhere else.
  const [password, setPassword] = useState('');
  const [manual, setManual] = useState<NodeManualProvisionResponse | null>(null);
  const [provisioned, setProvisioned] = useState<NodeProvisionResponse | null>(null);
  const [allocCpu, setAllocCpu] = useState(1);
  const [allocMem, setAllocMem] = useState(1);
  const [allocGpuGb, setAllocGpuGb] = useState(0);
  const [busy, setBusy] = useState<null | 'connect' | 'provision' | 'manual' | 'verify' | 'register'>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const reset = () => {
    setStep('form');
    setConnectInfo(null);
    setPassword('');
    setManual(null);
    setProvisioned(null);
    setNotice(null);
  };

  const onProvisioned = (res: NodeProvisionResponse) => {
    setProvisioned(res);
    // Start at half the machine (as VS Code does): safe on a computer someone
    // also uses. The sliders go up to everything.
    setAllocCpu(Math.max(1, Math.floor(res.detected_cpu / 2)));
    const { min, max } = memBounds(res.detected_memory_gb);
    setAllocMem(Math.max(min, Math.floor(max) / 2));
    setAllocGpuGb(Math.round(gpuTotalGb(res) * 0.25 * 2) / 2);
    setNotice(null);
    setStep('detected');
  };

  const hostKeyMismatchNotice: Notice = {
    tone: 'error',
    title: 'Host key changed',
    body:
      'The machine answered with a different host key than the one you confirmed. This can mean the machine was ' +
      'reinstalled, or that something is intercepting the connection. Nothing was sent. Check the machine, then connect again.',
  };

  const handleConnect = async () => {
    if (!form.host.trim() || !form.username.trim()) {
      setNotice({ tone: 'error', title: 'Missing fields', body: 'Host/IP and username are required.' });
      return;
    }
    setBusy('connect');
    setNotice(null);
    try {
      const res = await sshApi.connect({ host: form.host.trim(), port: form.port, username: form.username.trim() });
      setConnectInfo(res);
      setStep('confirm');
    } catch (e) {
      setNotice({ tone: 'error', title: 'Could not reach the machine', body: errorMessage(e, 'Connection failed') });
    } finally {
      setBusy(null);
    }
  };

  const startManual = async (reason: Notice) => {
    if (!connectInfo) return;
    setBusy('manual');
    try {
      const res = await sshApi.provisionManual({ onboarding_token: connectInfo.onboarding_token });
      setManual(res);
      setStep('manual');
      setNotice(reason);
    } catch (e) {
      if (e instanceof ApiError && e.mentions('host_key_mismatch')) {
        setNotice(hostKeyMismatchNotice);
        setStep('form');
      } else {
        setNotice({ tone: 'error', title: 'Manual setup failed', body: errorMessage(e) });
      }
    } finally {
      setBusy(null);
    }
  };

  const handleProvision = async () => {
    if (!connectInfo || !password) return;
    const pw = password;
    setPassword(''); // clear immediately; the request below is the only use
    setBusy('provision');
    setNotice(null);
    try {
      const res = await sshApi.provision({ onboarding_token: connectInfo.onboarding_token, password: pw });
      onProvisioned(res);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setNotice({ tone: 'error', title: 'Wrong password', body: 'The machine rejected the password. Try again.' });
      } else if (e instanceof ApiError && e.mentions('password_auth_disabled')) {
        await startManual({
          tone: 'warning',
          title: 'This machine does not accept password logins',
          body: 'Run the command below once on the machine to add the access key, then press "I ran it — verify".',
        });
      } else if (e instanceof ApiError && e.mentions('host_key_mismatch')) {
        setConnectInfo(null);
        setStep('form');
        setNotice(hostKeyMismatchNotice);
      } else {
        setNotice({ tone: 'error', title: 'Setup failed', body: errorMessage(e) });
      }
    } finally {
      setBusy(null);
    }
  };

  const handleVerify = async () => {
    if (!manual) return;
    setBusy('verify');
    try {
      onProvisioned(await sshApi.verifyProvision(manual.provision_id));
      setManual(null);
    } catch (e) {
      if (e instanceof ApiError && e.mentions('host_key_mismatch')) {
        setNotice(hostKeyMismatchNotice);
      } else {
        setNotice({
          tone: 'error',
          title: 'Key not found yet',
          body: `${errorMessage(e)} — make sure the command ran as ${connectInfo?.username ?? 'the same user'} without errors.`,
        });
      }
    } finally {
      setBusy(null);
    }
  };

  const handleRegister = async () => {
    if (!provisioned) return;
    setBusy('register');
    try {
      const node = await sshApi.register({
        provision_id: provisioned.provision_id,
        name: form.name.trim(),
        allocated_cpu: allocCpu,
        allocated_memory_gb: allocMem,
        ...gpuAllocation(provisioned, allocGpuGb),
      });
      showSuccess(`Worker "${node.name}" added`);
      setForm({ name: '', host: '', port: 22, username: '' });
      reset();
      onRegistered();
    } catch (e) {
      showError(errorMessage(e, 'Registration failed'));
    } finally {
      setBusy(null);
    }
  };

  const formLocked = step !== 'form';

  return (
    <Card>
      <div className="flex items-center gap-2 mb-1">
        <Plus size={18} className="text-blue-600" />
        <h2 className="text-lg font-semibold text-slate-900">Add a worker</h2>
      </div>
      <p className="text-sm text-slate-500 mb-5">
        The worker only needs an SSH server and Python 3; nothing is installed on it, and your password is used once to
        add a restricted access key, then discarded.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label htmlFor="onb-name" className="block text-sm font-medium mb-1 text-slate-700">
            Name <span className="text-slate-500 font-normal">(optional)</span>
          </label>
          <input
            id="onb-name"
            className={INPUT_CLS}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="lab-box-1"
          />
        </div>
        <div>
          <label htmlFor="onb-host" className="block text-sm font-medium mb-1 text-slate-700">Host / IP</label>
          <input
            id="onb-host"
            className={INPUT_CLS}
            disabled={formLocked}
            value={form.host}
            onChange={(e) => setForm({ ...form, host: e.target.value })}
            placeholder="192.168.1.100"
          />
        </div>
        <div>
          <label htmlFor="onb-port" className="block text-sm font-medium mb-1 text-slate-700">Port</label>
          <fieldset disabled={formLocked}>
            <NumberField
              id="onb-port"
              value={form.port}
              min={1}
              max={65535}
              step={1}
              fallback={22}
              onChange={(n) => setForm({ ...form, port: Math.round(n) })}
              className={INPUT_CLS}
            />
          </fieldset>
        </div>
        <div>
          <label htmlFor="onb-user" className="block text-sm font-medium mb-1 text-slate-700">Username</label>
          <input
            id="onb-user"
            className={INPUT_CLS}
            disabled={formLocked}
            value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })}
            placeholder="alice"
            autoComplete="off"
          />
        </div>
      </div>

      {step === 'form' && (
        <div className="mt-5">
          <PrimaryButton onClick={handleConnect} disabled={busy === 'connect'}>
            <span className="flex items-center gap-2">
              {busy === 'connect' && <Loader2 size={16} className="animate-spin" />}
              {busy === 'connect' ? `Connecting to ${form.host}…` : 'Connect'}
            </span>
          </PrimaryButton>
        </div>
      )}

      {notice && <NoticeBox notice={notice} />}

      {/* Step 2: confirm host key, then password ------------------------- */}
      {step === 'confirm' && connectInfo && (
        <div className="mt-6 border-t pt-5 space-y-4">
          <div className="rounded-xl border border-blue-200 bg-blue-50/60 p-4">
            <div className="flex items-center gap-2 font-semibold text-slate-900 mb-1">
              <Fingerprint size={16} className="text-blue-600" /> Confirm this is your machine
            </div>
            <p className="text-sm text-slate-600 mb-2">
              {connectInfo.username}@{connectInfo.host}:{connectInfo.port} presented this {connectInfo.host_key_type} host
              key. Compare it with <code className="font-mono text-xs">ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub</code> on
              the machine (on Windows: <code className="font-mono text-xs">C:\ProgramData\ssh</code>).
            </p>
            <div className="font-mono text-sm text-slate-900 bg-white border border-blue-200 rounded-lg px-3 py-2 break-all">
              {connectInfo.fingerprint}
            </div>
            {connectInfo.already_registered && (
              <div className="mt-3 flex gap-2 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                <AlertTriangle size={16} className="shrink-0 mt-0.5" />
                One of your workers already uses this exact host key. Adding it again creates a duplicate worker.
              </div>
            )}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleProvision();
            }}
            autoComplete="off"
          >
            <label htmlFor="onb-password" className="block text-sm font-medium mb-1 text-slate-700">
              Password for {connectInfo.username}
            </label>
            <input
              id="onb-password"
              type="password"
              autoComplete="off"
              name="worker-one-time-password"
              className={`${INPUT_CLS} max-w-sm`}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <p className="text-xs text-slate-500 mt-1">Used once over the confirmed connection; never stored.</p>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <PrimaryButton type="submit" disabled={!password || busy !== null}>
                <span className="flex items-center gap-2">
                  {busy === 'provision' && <Loader2 size={16} className="animate-spin" />}
                  {busy === 'provision' ? 'Adding key and detecting resources…' : 'Confirm & Add'}
                </span>
              </PrimaryButton>
              <SecondaryButton onClick={reset} disabled={busy !== null}>
                Cancel
              </SecondaryButton>
              <button
                type="button"
                className="text-xs text-blue-600 hover:text-blue-800"
                disabled={busy !== null}
                onClick={() =>
                  startManual({
                    tone: 'warning',
                    title: 'Manual key setup',
                    body: 'Run the command below once on the machine, then press "I ran it — verify".',
                  })
                }
              >
                Can't log in with a password? Set up the key manually
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Step 2b: manual fallback ----------------------------------------- */}
      {step === 'manual' && manual && (
        <div className="mt-6 border-t pt-5 space-y-4">
          <div>
            <div className="text-sm font-medium text-slate-700 mb-1">Public key that will be added</div>
            <CopyBlock text={manual.public_key} />
          </div>
          <div>
            <div className="text-sm font-medium text-slate-700 mb-1">Linux / macOS (run as {connectInfo?.username ?? 'the worker user'})</div>
            <CopyBlock text={manual.install_command_posix} />
          </div>
          <div>
            <div className="text-sm font-medium text-slate-700 mb-1">Windows PowerShell</div>
            <CopyBlock text={manual.install_command_windows} />
          </div>
          <p className="text-xs text-slate-500">
            Valid until {new Date(manual.expires_at).toLocaleString()}. The key is restricted (no forwarding, no
            terminal) and tagged <code className="font-mono">dp-platform:{manual.provision_id}</code> so you can find and
            remove it later.
          </p>
          <div className="flex gap-3">
            <PrimaryButton onClick={handleVerify} disabled={busy !== null}>
              <span className="flex items-center gap-2">
                {busy === 'verify' && <Loader2 size={16} className="animate-spin" />}
                {busy === 'verify' ? 'Verifying…' : 'I ran it — verify'}
              </span>
            </PrimaryButton>
            <SecondaryButton onClick={reset} disabled={busy !== null}>
              Cancel
            </SecondaryButton>
          </div>
        </div>
      )}

      {/* Step 3: detected resources + allocation ------------------------- */}
      {step === 'detected' && provisioned && (
        <div className="mt-6 border-t pt-5">
          <h3 className="font-semibold text-slate-900 mb-3 flex items-center gap-2">
            <ShieldCheck size={16} className="text-emerald-600" /> Key installed · detected resources
          </h3>
          <div className="grid grid-cols-3 gap-4 mb-4 text-sm">
            <Stat label="CPU cores" value={String(provisioned.detected_cpu)} />
            <Stat label="Memory (GB)" value={String(provisioned.detected_memory_gb)} />
            <Stat label="GPUs" value={String(provisioned.detected_gpu)} />
          </div>
          {provisioned.os_info && <p className="text-xs text-slate-500 mb-2">OS: {provisioned.os_info}</p>}
          <CapabilityChips caps={provisioned} runtimeNames={runtimeNames} />
          <div className="space-y-4 mt-5">
            <div>
              <label htmlFor="onb-cpu" className="block text-sm font-medium text-slate-700 mb-1">
                Cores to lend: {allocCpu} / {provisioned.detected_cpu}
              </label>
              <input
                id="onb-cpu"
                type="range"
                min={1}
                max={Math.max(1, provisioned.detected_cpu)}
                value={allocCpu}
                onChange={(e) => setAllocCpu(parseInt(e.target.value, 10))}
                className="w-full accent-blue-600"
              />
            </div>
            <div>
              <label htmlFor="onb-mem" className="block text-sm font-medium text-slate-700 mb-1">
                Memory to lend (GB): {allocMem} / {provisioned.detected_memory_gb}
              </label>
              <input
                id="onb-mem"
                type="range"
                min={memBounds(provisioned.detected_memory_gb).min}
                max={memBounds(provisioned.detected_memory_gb).max}
                step={0.5}
                value={allocMem}
                onChange={(e) => setAllocMem(parseFloat(e.target.value))}
                className="w-full accent-blue-600"
              />
            </div>
            <GpuSliceControl caps={provisioned} value={allocGpuGb} onChange={setAllocGpuGb} />
          </div>
          <div className="mt-5 flex gap-3">
            <PrimaryButton onClick={handleRegister} disabled={busy !== null}>
              {busy === 'register' ? 'Registering…' : 'Register'}
            </PrimaryButton>
            <SecondaryButton onClick={reset} disabled={busy !== null}>
              Cancel
            </SecondaryButton>
          </div>
          <p className="text-xs text-slate-500 mt-2">
            Offer valid until {new Date(provisioned.expires_at).toLocaleString()}.
          </p>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// One registered worker
// ---------------------------------------------------------------------------
type RowAction = 'health' | 'refresh' | 'rotate' | 'resecure' | 'save' | 'delete' | 'drain';

function NodeRow({
  node,
  runtimeNames,
  onUpdated,
  onDeleted,
}: {
  node: NodeResponse;
  runtimeNames: Record<string, string>;
  onUpdated: (n: NodeResponse) => void;
  onDeleted: () => void;
}) {
  const { showSuccess, showError } = useToast();
  const [busy, setBusy] = useState<RowAction | null>(null);
  const [editing, setEditing] = useState(false);
  const editFromNode = () => ({
    name: node.name,
    cpu: node.allocated_cpu,
    mem: node.allocated_memory_gb,
    gpuGb: node.allocated_gpu_memory_gb ?? 0,
    envCacheGb: node.env_cache_gb ?? 0,
  });
  const [edit, setEdit] = useState(editFromNode);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [resecureOpen, setResecureOpen] = useState(false);

  const managed = node.auth_type === 'managed_key';
  const draining = node.status === 'draining';

  const run = async (action: RowAction, fn: () => Promise<NodeResponse>, ok: (n: NodeResponse) => string) => {
    setBusy(action);
    try {
      const updated = await fn();
      onUpdated(updated);
      showSuccess(ok(updated));
      return true;
    } catch (e) {
      showError(errorMessage(e));
      return false;
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    setConfirmDelete(false);
    setBusy('delete');
    try {
      const res = await sshApi.deleteNode(node.id);
      if (res.key_removed) {
        showSuccess(res.message || `"${node.name}" removed and its access key deleted from the machine`);
      } else {
        showError(
          res.message ||
            `"${node.name}" removed, but the machine was unreachable: delete the authorized_keys line ending in dp-platform:${node.id} yourself.`,
        );
      }
      onDeleted();
    } catch (e) {
      showError(errorMessage(e, 'Delete failed'));
      setBusy(null);
    }
  };

  const saveEdit = async () => {
    const ok = await run(
      'save',
      () =>
        sshApi.updateNode(node.id, {
          name: edit.name.trim() || null,
          allocated_cpu: edit.cpu,
          allocated_memory_gb: edit.mem,
          ...gpuAllocation(node, edit.gpuGb),
          env_cache_gb: edit.envCacheGb,
        }),
      (n) => `"${n.name}" updated`,
    );
    if (ok) setEditing(false);
  };

  const iconBtn =
    'p-1.5 rounded-md text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
  const mem = memBounds(node.detected_memory_gb);

  return (
    <div className="border border-slate-200 rounded-lg px-4 py-3">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2 sm:gap-3">
        <div className="min-w-0">
          <div className="font-medium text-slate-900 flex flex-wrap items-center gap-2">
            {node.name}
            <span className="text-slate-500 font-normal text-sm">
              {node.username}@{node.host}:{node.port}
            </span>
            {managed ? (
              <span
                className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200"
                title={node.key_created_at ? `Key created ${new Date(node.key_created_at).toLocaleString()}` : undefined}
              >
                <ShieldCheck size={11} /> Managed key
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200">
                <ShieldAlert size={11} /> Legacy {node.auth_type === 'private_key' ? 'private key' : 'password'}
              </span>
            )}
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            {sliceSummary(node)} · {node.allocated_memory_gb} / {node.detected_memory_gb} GB RAM
            {node.env_cache_gb ? ` · ${node.env_cache_gb} GB env cache` : ''}
            {' · '}health {node.health_status}
            {node.last_health_check ? ` (${new Date(node.last_health_check).toLocaleString()})` : ''}
          </div>
          {gpuCount(node) > 0 && !node.gpu_runtime && (
            <div className="text-xs text-amber-700 mt-0.5 flex items-center gap-1">
              <AlertTriangle size={11} /> torch not found — GPU slice not enforceable
            </div>
          )}
          {node.fingerprint && (
            <div className="text-[11px] text-slate-500 font-mono mt-0.5 truncate" title={node.fingerprint}>
              {node.fingerprint}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1 shrink-0">
          <StatusBadge status={node.status} />
          {!managed && (
            <button
              onClick={() => setResecureOpen(true)}
              disabled={busy !== null}
              className="ml-1 text-xs font-medium px-2 py-1 rounded-md bg-amber-100 text-amber-800 hover:bg-amber-200 disabled:opacity-40"
            >
              Re-secure
            </button>
          )}
          <button
            className={iconBtn}
            title="Health check"
            disabled={busy !== null}
            onClick={() => run('health', () => sshApi.healthCheck(node.id), (n) => `${n.name}: ${n.health_status}`)}
          >
            {busy === 'health' ? <Loader2 size={15} className="animate-spin" /> : <Activity size={15} />}
          </button>
          <button
            className={iconBtn}
            title="Re-detect resources and languages"
            disabled={busy !== null}
            onClick={() => run('refresh', () => sshApi.refresh(node.id), (n) => `${n.name}: ${Object.keys(n.runtimes).length} languages detected`)}
          >
            {busy === 'refresh' ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
          </button>
          {managed && (
            <button className={iconBtn} title="Rotate access key" disabled={busy !== null} onClick={() => setConfirmRotate(true)}>
              {busy === 'rotate' ? <Loader2 size={15} className="animate-spin" /> : <RotateCw size={15} />}
            </button>
          )}
          <button
            className={iconBtn}
            title={draining ? 'Resume: take new work again' : 'Drain: finish running work, take no new work'}
            aria-label={draining ? 'Resume worker' : 'Drain worker'}
            disabled={busy !== null}
            onClick={() =>
              run(
                'drain',
                () => sshApi.updateNode(node.id, { status: draining ? 'online' : 'draining' }),
                (n) => (n.status === 'draining' ? `${n.name} is draining: it gets no new tasks or training` : `${n.name} takes new work again`),
              )
            }
          >
            {busy === 'drain' ? <Loader2 size={15} className="animate-spin" /> : draining ? <PlayCircle size={15} /> : <PauseCircle size={15} />}
          </button>
          <button
            className={iconBtn}
            title="Edit name and allocation"
            disabled={busy !== null}
            onClick={() => {
              setEdit(editFromNode());
              setEditing((v) => !v);
            }}
          >
            <Pencil size={15} />
          </button>
          <button
            className={`${iconBtn} hover:text-rose-600`}
            title="Remove worker"
            disabled={busy !== null}
            onClick={() => setConfirmDelete(true)}
          >
            {busy === 'delete' ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
          </button>
        </div>
      </div>

      <div className="mt-2">
        <CapabilityChips caps={node} runtimeNames={runtimeNames} />
      </div>

      {editing && (
        <div className="mt-3 border-t border-slate-100 pt-3 grid grid-cols-1 sm:grid-cols-3 gap-4 items-end">
          <label className="text-sm">
            <span className="text-xs text-slate-500 font-medium block mb-1">Name</span>
            <input className={INPUT_CLS} value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
          </label>
          <label className="text-sm">
            <span className="text-xs text-slate-500 font-medium block mb-1">
              Cores: {edit.cpu} / {node.detected_cpu}
            </span>
            <input
              type="range"
              min={1}
              max={Math.max(1, node.detected_cpu)}
              value={edit.cpu}
              onChange={(e) => setEdit({ ...edit, cpu: parseInt(e.target.value, 10) })}
              className="w-full accent-blue-600"
            />
          </label>
          <label className="text-sm">
            <span className="text-xs text-slate-500 font-medium block mb-1">
              Memory: {edit.mem} / {node.detected_memory_gb} GB
            </span>
            <input
              type="range"
              min={mem.min}
              max={mem.max}
              step={0.5}
              value={edit.mem}
              onChange={(e) => setEdit({ ...edit, mem: parseFloat(e.target.value) })}
              className="w-full accent-blue-600"
            />
          </label>
          <div className="sm:col-span-3">
            <GpuSliceControl caps={node} value={edit.gpuGb} onChange={(gpuGb) => setEdit({ ...edit, gpuGb })} />
          </div>
          <label className="text-sm sm:col-span-3">
            <span className="text-xs text-slate-500 font-medium block mb-1">
              Environment cache for projects: {edit.envCacheGb ? `${edit.envCacheGb} GB` : 'off'}
            </span>
            <input
              type="range"
              min={0}
              max={50}
              step={5}
              value={edit.envCacheGb}
              aria-label="Environment cache (GB)"
              onChange={(e) => setEdit({ ...edit, envCacheGb: parseFloat(e.target.value) })}
              className="w-full accent-blue-600"
            />
            <span className="text-xs text-slate-500 block mt-0.5">
              Off: each project task builds its Python environment in its sandbox and nothing stays on the machine. On:
              environments are kept in ~/.cache/distributed-compute for later tasks, and deleted when the worker is removed.
            </span>
          </label>
          <div className="sm:col-span-3 flex gap-2">
            <PrimaryButton onClick={saveEdit} disabled={busy !== null}>
              {busy === 'save' ? 'Saving…' : 'Save'}
            </PrimaryButton>
            <SecondaryButton onClick={() => setEditing(false)}>Cancel</SecondaryButton>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmDelete}
        title={`Remove ${node.name}?`}
        message="The platform's access key is removed from the machine (if it is reachable) and the worker is deleted. Running tasks on it are stopped."
        onConfirm={handleDelete}
        onCancel={() => setConfirmDelete(false)}
      />
      <ConfirmDialog
        open={confirmRotate}
        title="Rotate access key?"
        message="A new key is installed and verified, then the old key line is removed from the machine."
        onConfirm={() => {
          setConfirmRotate(false);
          run('rotate', () => sshApi.rotateKey(node.id), (n) => `${n.name}: access key rotated`);
        }}
        onCancel={() => setConfirmRotate(false)}
      />
      {resecureOpen && (
        <ResecureDialog
          node={node}
          onClose={() => setResecureOpen(false)}
          onSubmit={async (password) => {
            const ok = await run(
              'resecure',
              () => sshApi.resecure(node.id, { password: password || null }),
              (n) => `${n.name} now uses a managed key; the old credential was wiped`,
            );
            if (ok) setResecureOpen(false);
          }}
          busy={busy === 'resecure'}
        />
      )}
    </div>
  );
}

function ResecureDialog({
  node,
  busy,
  onClose,
  onSubmit,
}: {
  node: NodeResponse;
  busy: boolean;
  onClose: () => void;
  onSubmit: (password: string) => Promise<void>;
}) {
  const [password, setPassword] = useState('');
  const submit = async () => {
    const pw = password;
    setPassword('');
    await onSubmit(pw);
  };
  return (
    <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center z-50 px-4">
      <form
        className="bg-white rounded-2xl p-6 w-full max-w-sm border border-slate-200 shadow-xl"
        autoComplete="off"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="flex items-start justify-between mb-2">
          <h3 className="text-lg font-semibold text-slate-900 flex items-center gap-2">
            <KeyRound size={18} className="text-amber-600" /> Re-secure {node.name}
          </h3>
          <button type="button" onClick={onClose} className="text-slate-500 hover:text-slate-600">
            <X size={16} />
          </button>
        </div>
        <p className="text-slate-600 text-sm mb-4">
          Replaces the stored {node.auth_type === 'private_key' ? 'private key' : 'password'} with a platform-managed,
          restricted key and wipes the old secret. Enter the password for {node.username}; if you leave it empty, the
          credential saved when this worker was added is used once.
        </p>
        <input
          type="password"
          autoComplete="off"
          name="worker-resecure-password"
          className={INPUT_CLS}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password (optional)"
        />
        <div className="flex justify-end gap-3 mt-5">
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
          <PrimaryButton type="submit" disabled={busy}>
            {busy ? 'Re-securing…' : 'Re-secure'}
          </PrimaryButton>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------
function CapabilityChips({ caps, runtimeNames }: { caps: NodeCapabilities; runtimeNames: Record<string, string> }) {
  // Python has its own chip; every other detected language gets one chip.
  const languages = Object.entries(caps.runtimes ?? {})
    .filter(([id]) => id !== 'python')
    .sort(([a], [b]) => a.localeCompare(b));
  const chip = 'inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border';
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {caps.os_type && <span className={`${chip} bg-slate-100 text-slate-600 border-slate-200`}>{caps.os_type}</span>}
      <span className={`${chip} bg-indigo-50 text-indigo-700 border-indigo-200`} title="How CPU/memory limits are enforced">
        {caps.resource_enforcement}
      </span>
      {(caps.gpu_vendor || caps.gpu_model) && (
        <span className={`${chip} bg-emerald-50 text-emerald-700 border-emerald-200`}>
          <Boxes size={11} /> {caps.gpu_model || caps.gpu_vendor}
          {caps.gpu_count > 1 ? ` ×${caps.gpu_count}` : ''}
        </span>
      )}
      <span
        className={`${chip} ${caps.python_version ? 'bg-blue-50 text-blue-700 border-blue-200' : 'bg-rose-50 text-rose-700 border-rose-200'}`}
      >
        <Cpu size={11} /> {caps.python_version ? `Python ${caps.python_version}` : 'No Python 3'}
      </span>
      {languages.length === 0 ? (
        <span className={`${chip} bg-slate-50 text-slate-500 border-slate-200`}>no other languages detected</span>
      ) : (
        languages.map(([id, version]) => (
            <span key={id} className={`${chip} bg-sky-50 text-sky-700 border-sky-200`} title={version}>
              {runtimeNames[id] ?? id}
              {shortVersion(runtimeNames[id] ?? id, version) ? (
                <span className="text-sky-600">{shortVersion(runtimeNames[id] ?? id, version)}</span>
              ) : null}
            </span>
          ))
      )}
    </div>
  );
}

function CopyBlock({ text }: { text: string }) {
  const { showError } = useToast();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      showError('Copy failed. Select the text and copy it manually.');
    }
  };
  return (
    <div className="relative">
      <pre className="bg-[#0f172a] text-slate-100 text-xs font-mono p-3 pr-12 rounded-lg overflow-auto whitespace-pre-wrap break-all max-h-40">
        {text}
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute top-2 right-2 p-1.5 rounded-md bg-slate-700 text-slate-200 hover:bg-slate-600"
        title="Copy"
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>
    </div>
  );
}

function NoticeBox({ notice }: { notice: NonNullable<Notice> }) {
  const styles =
    notice.tone === 'error' ? 'bg-rose-50 border-rose-200 text-rose-800' : 'bg-amber-50 border-amber-200 text-amber-800';
  return (
    <div className={`mt-4 flex gap-2 rounded-lg border px-3.5 py-2.5 text-sm ${styles}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
      <AlertTriangle size={16} className="shrink-0 mt-0.5" />
      <div>
        <div className="font-semibold">{notice.title}</div>
        <div>{notice.body}</div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 text-center">
      <div className="text-2xl font-bold text-slate-900">{value}</div>
      <div className="text-xs text-slate-500 mt-1">{label}</div>
    </div>
  );
}
