import {Client} from './client.js';
import {Debug} from './debug.js';
import {commandDeadlineMs} from './execution.js';
import * as api from './operations/index.js';
import * as models from './models/index.js';
import type {CreateOptions, HostLogOptions, RunOptions, VmSelector} from './options.js';
import {Transport, type CallOptions, type TransportOptions} from './transport.js';
import {Mcp, Networks} from './resources.js';
import {VM} from './vm.js';

const LABEL_KEY_RE = /^[A-Za-z0-9._/-]{1,63}$/;
const CONTROL_CHAR_RE = /[\x00-\x1f\x7f-\x9f]/;

function validateLabels(labels: Record<string, string> | undefined): Record<string, string> | undefined {
  if (labels === undefined) return undefined;
  const entries = Object.entries(labels);
  if (entries.length === 0) return undefined;
  if (entries.length > 64) throw new TypeError('labels exceeds maximum of 64 entries');
  for (const [key, value] of entries) {
    if (!LABEL_KEY_RE.test(key) || new TextEncoder().encode(value).byteLength > 255 || CONTROL_CHAR_RE.test(value)) {
      throw new TypeError(`Invalid label "${key}"`);
    }
  }
  return labels;
}

function memoryMb(memory: number | undefined): number | null {
  if (memory === undefined) return null;
  if (!Number.isSafeInteger(memory) || memory <= 0) throw new TypeError('Memory must be a positive GiB count');
  const megabytes = memory * 1024;
  if (!Number.isSafeInteger(megabytes)) throw new TypeError('Memory is too large');
  return megabytes;
}

export class Hypervisor extends Client {
  readonly networks: Networks;
  /** The MCP servers every VM runs, from settings and corp config. */
  readonly mcp: Mcp;
  readonly debug: Debug;
  constructor(url: string, token: string, options: TransportOptions = {}) {
    const transport = new Transport(url, token, options);
    super(transport);
    this.networks = new Networks(transport);
    this.mcp = new Mcp(transport);
    this.debug = new Debug(transport);
  }
  async info(options: CallOptions = {}): Promise<models.HypervisorInfo> {
    return api.getHypervisorInfo(this.transport, options);
  }
  async list(options: CallOptions = {}): Promise<models.ListResponse> {
    return api.listVms(this.transport, options);
  }
  vm(selector: VmSelector): VM {
    return new VM(this.transport, selector);
  }
  async create(options: CreateOptions = {}): Promise<VM> {
    const labels = validateLabels(options.labels);
    if (options.cpus !== undefined && (!Number.isSafeInteger(options.cpus) || options.cpus < 1)) {
      throw new TypeError('cpus must be positive');
    }
    if (options.image === undefined && (options.command !== undefined || options.registry !== undefined)) {
      throw new TypeError('Container command and registry require an image');
    }
    if (options.image !== undefined && (typeof options.image !== 'string' || !options.image)) {
      throw new TypeError('Image must be a nonempty string');
    }
    const ram_mb = memoryMb(options.memory);
    const container = options.image === undefined ? undefined : {
      image: options.image,
      args: [...(options.command ?? [])],
      env: options.env ?? {},
      ...(options.registry === undefined ? {} : {registry: {...options.registry}}),
      attach: false,
    };
    const response = await api.createVm(this.transport, {body: {
      name: options.name || null, persistent: Boolean(options.name),
      cpus: options.cpus ?? null, ram_mb,
      env: container === undefined ? options.env ?? null : null,
      ...(labels === undefined ? {} : {labels}),
      networks: (options.networks ?? []).map(network => network.name),
      ...(container === undefined ? {} : {container}),
    }}, options);
    return VM.bind(this.transport, response.id, response.name, container !== undefined);
  }
  async log(options: HostLogOptions = {}): Promise<models.HostLogsResponse> {
    return api.getHypervisorLogs(this.transport, {...options, name: options.source ?? models.HostLogSource.SERVICE}, options);
  }
  async run(command: string, options: RunOptions = {}): Promise<models.ExecResponse> {
    const ram_mb = memoryMb(options.memory);
    return api.runVm(this.transport, {body: {
      command, timeout_secs: options.timeout_secs ?? null,
      cpus: options.cpus ?? null, ram_mb, env: options.env ?? null,
    }}, {...options, timeoutMs: options.timeoutMs ?? commandDeadlineMs(this.transport.timeoutMs, options.timeout_secs)});
  }
  async purge(options: CallOptions & {all?: boolean} = {}): Promise<models.PurgeResponse> {
    return api.purgeVms(this.transport, {body: {all: options.all ?? false}}, options);
  }
  async update(options: CallOptions = {}): Promise<models.UpdateActionResponse> {
    return api.updateHypervisor(this.transport, {body: {confirmed: true}}, options);
  }
  async restart(options: CallOptions = {}): Promise<models.RestartResponse> {
    return api.restartHypervisor(this.transport, options);
  }
}
