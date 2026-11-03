// Localhost integration and prepared opt-in factory. The app's default transport remains disabled.
import type { Auth } from 'firebase/auth';
import type { Firestore } from 'firebase/firestore/lite';
import { createLocalSparkIntake, createPreparedSparkIntake } from './sparkDeletionIntake';
import { accountDeletionTransport } from './accountDeletion';
import { compareDeletionTime, deletionTimeSeconds, parseDeletionReceipt, validAttempt,
  type DeletionAttempt, type DeletionReceipt, type DeletionTime, type DeletionTransport } from '../domain/deletion';

type RecordValue = Record<string, unknown>;
function record(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Firestore response');
  return value as RecordValue;
}
function text(value: unknown): string {
  if (typeof value !== 'string') throw new Error('Invalid Firestore string');
  return value;
}
export function parseFirestoreTime(value: unknown): DeletionTime {
  const raw = text(value), match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(raw);
  if (!match) throw new Error('Invalid server timestamp');
  const millis = Date.parse(match[1]+'Z'); // Parses the received server value; never reads the device clock.
  if (!Number.isFinite(millis) || millis < 0 || new Date(millis).toISOString().slice(0,19) !== match[1]) throw new Error('Invalid server date');
  return {seconds: millis / 1000, nanoseconds: Number((match[2] || '').padEnd(9,'0'))};
}

// batchGet.readTime is server observation time. updateTime is not a current clock.
export function sparkReceiptFromBatchGet(value: unknown, documentName: string): DeletionReceipt | null {
  if (!Array.isArray(value) || value.length !== 1) throw new Error('Invalid scoped read');
  const envelope = record(value[0]), serverNow = parseFirestoreTime(envelope.readTime);
  if ('missing' in envelope) {
    if (envelope.missing !== documentName || 'found' in envelope) throw new Error('Wrong current account');
    return null;
  }
  const document = record(envelope.found);
  if (document.name !== documentName) throw new Error('Wrong current account');
  const f = record(document.fields), fieldText = (key: string) => text(record(f[key]).stringValue);
  if (record(f.version).integerValue !== '2') throw new Error('Unsupported intent version');
  const requestId = fieldText('requestId');
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(requestId)) throw new Error('Invalid request identifier');
  const requestedAt = parseFirestoreTime(record(f.requestedAt).timestampValue);
  const before = compareDeletionTime(serverNow, {...requestedAt, seconds: requestedAt.seconds+604800}) < 0;
  let status = fieldText('status') as DeletionReceipt['status'];
  if (!['grace_period','cancel_requested','cancelled','processing','retry_wait','needs_review','completed'].includes(status)) throw new Error('Invalid intent state');
  if (status === 'grace_period' && !before) status = 'queued';
  const phase = f.phase === undefined ? 'quiesce' : fieldText('phase') as DeletionReceipt['phase'];
  let completedScope: string[] = [];
  if (f.completedScope !== undefined) {
    const values = record(record(f.completedScope).arrayValue).values ?? [];
    if (!Array.isArray(values)) throw new Error('Invalid completion evidence');
    completedScope = values.map(item => text(record(item).stringValue));
  }
  const accepted = deletionTimeSeconds(requestedAt);
  return parseDeletionReceipt({version:2,requestId,status,phase,requestedAt:accepted,deleteAfter:accepted+604800,
    serverNow:deletionTimeSeconds(serverNow),cancelAllowed:status==='grace_period' && before,
    preciseTime:{requestedAt,serverNow},completedScope,subscriptionCancelled:false}, requestId);
}

type Options = {
  auth: Auth; db: Firestore;
  storage: {load(): Promise<DeletionAttempt | null>; save(value: DeletionAttempt): Promise<void>};
  crypto: {randomUUID(): string; getRandomValues(value: Uint8Array): Uint8Array};
};
export function createLocalSparkDeletionTransport(options: Options): DeletionTransport {
  const {auth, db} = options;
  const intake = createLocalSparkIntake(auth, db); // Validates demo project and exact Auth/Firestore loopback binding.
  return buildTransport(options,intake,'demo-dailypick10-deletion','http://127.0.0.1:18080','emulator');
}

export function createPreparedSparkDeletionTransport(options: Options & {expectedProjectId: string; enabled?: boolean}): DeletionTransport {
  if(options.enabled!==true)return accountDeletionTransport; // No Auth, Firestore, storage or network access while off.
  const intake=createPreparedSparkIntake(options.auth,options.db,options.expectedProjectId,true);
  return buildTransport(options,intake,options.expectedProjectId,'https://firestore.googleapis.com','live');
}

function buildTransport(options: Options, intake: ReturnType<typeof createLocalSparkIntake>, project: string, endpoint: string, mode: 'emulator' | 'live'): DeletionTransport {
  const {auth}=options;
  let saved: DeletionAttempt | null = null;
  const identity = (uid?: string) => {
    const user = auth.currentUser;
    if (!user || !user.uid || user.uid.includes('/') || user.uid.length > 128 || (uid && user.uid !== uid)) throw new Error('Account changed');
    return user;
  };
  const current = async () => {
    const user = identity(), token = await user.getIdToken(true);
    if (!token || auth.currentUser !== user) throw new Error('Account changed');
    const name = 'projects/'+project+'/databases/(default)/documents/accountDeletionIntents/'+user.uid;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),8000);
    try {
      const response = await fetch(endpoint+'/v1/projects/'+project+'/databases/(default)/documents:batchGet', {
        method:'POST', headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
        body:JSON.stringify({documents:[name]}),redirect:'error',credentials:'omit',cache:'no-store',signal:controller.signal,
      });
      if (!response.ok || !response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Authenticated status unavailable');
      const body = await response.text();
      if (body.length > 16384 || auth.currentUser !== user) throw new Error('Invalid scoped response');
      return sparkReceiptFromBatchGet(JSON.parse(body),name);
    } finally {clearTimeout(timeout);}
  };
  return {
    mode, supportsRecoveryStatus:false, current,
    async load() {
      const value = await options.storage.load();
      if (value !== null && !validAttempt(value)) throw new Error('Invalid saved attempt');
      saved = value ? {...value} : null; return saved ? {...saved} : null;
    },
    async save(value) {
      if (!validAttempt(value)) throw new Error('Invalid saved attempt');
      identity(value.uid); await options.storage.save({...value}); saved={...value};
    },
    prepare(uid) {
      identity(uid);
      // Compatibility with the existing client journal. This value is local-only,
      // is never sent to Firestore, and cannot authorize an anonymous read.
      const bytes=options.crypto.getRandomValues(new Uint8Array(32));
      return {uid,requestId:options.crypto.randomUUID(),recoveryKey:Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')};
    },
    async submit(body) {
      if (body.version!==1 || !saved || body.requestId!==saved.requestId || body.recoveryKey!==saved.recoveryKey) throw new Error('Saved attempt required');
      const user=identity(saved.uid);
      await intake.request(body.requestId,body.confirmation);
      if(auth.currentUser!==user)throw new Error('Account changed');
      return current();
    },
    async cancel(body) {
      if(body.version!==1)throw new Error('Invalid request version');
      const user=identity();await intake.cancel(body.requestId,body.confirmation);
      if(auth.currentUser!==user)throw new Error('Account changed');
      return current();
    },
    async status() {throw new Error('No anonymous completion lookup');},
  };
}
