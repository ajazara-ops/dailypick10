import { validAttempt, type DeletionAttempt, type DeletionTransport } from '../domain/deletion';
const unavailable = () => { throw new Error('Account deletion backend is not connected'); };
// Intentionally no URL, Firebase write, Admin SDK, credential or environment toggle.
// A production transport and secure recovery-proof storage need separate review.
export const accountDeletionTransport: DeletionTransport = {
  mode: 'disabled', load: async () => null, save: async () => unavailable(),
  prepare: unavailable, submit: async () => unavailable(), status: async () => unavailable(),
  current: async () => unavailable(), cancel: async () => unavailable(),
};

type EmulatorOptions = {
  projectId: 'demo-dailypick10-deletion';
  baseUrl: 'http://127.0.0.1:18781';
  auth: { currentUser: { uid: string; getIdToken(forceRefresh?: boolean): Promise<string> } | null };
  storage: { load(): Promise<DeletionAttempt | null>; save(value: DeletionAttempt): Promise<void> };
  crypto: { randomUUID(): string; getRandomValues(array: Uint8Array): Uint8Array };
};

// Explicit local integration factory. Never selected by app environment variables.
// The default export above remains disabled; it cannot acquire a production URL.
export function createEmulatorDeletionTransport(options: EmulatorOptions): DeletionTransport {
  if (options.projectId !== 'demo-dailypick10-deletion' || options.baseUrl !== 'http://127.0.0.1:18781') {
    throw new Error('Exact local demo endpoint required');
  }
  let saved: DeletionAttempt | null = null;
  const call = async (route: string, body?: object, authenticated = true, expectedUid?: string): Promise<unknown | null> => {
    const headers: Record<string,string> = {'Content-Type':'application/json'};
    if (authenticated) {
      const user = options.auth.currentUser;
      if (!user || (expectedUid && user.uid !== expectedUid)) throw new Error('Account changed');
      const token = await user.getIdToken();
      if (options.auth.currentUser !== user || !token) throw new Error('Account changed');
      headers.Authorization = `Bearer ${token}`;
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(`${options.baseUrl}/__emulator__/v1/account-deletion/${route}`, {
        method: body === undefined ? 'GET' : 'POST', headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: 'error', credentials: 'omit', cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Local deletion request failed (${response.status})`);
      if (!response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Invalid server response');
      const text = await response.text();
      if (text.length > 16384) throw new Error('Invalid server response');
      const envelope = JSON.parse(text);
      if (!envelope || typeof envelope !== 'object' || !Object.prototype.hasOwnProperty.call(envelope, 'receipt')) throw new Error('Invalid server receipt');
      return envelope.receipt;
    } finally { clearTimeout(timeout); }
  };
  return {
    mode: 'emulator',
    supportsRecoveryStatus: true, // Existing localhost proof endpoint; never a public Firestore rule.
    async load() {
      const value = await options.storage.load();
      if (value !== null && !validAttempt(value)) throw new Error('Invalid recovery record');
      saved = value ? {...value} : null; return saved ? {...saved} : null;
    },
    async save(value) {
      if (!validAttempt(value) || options.auth.currentUser?.uid !== value.uid) throw new Error('Invalid recovery record');
      await options.storage.save({...value}); saved = {...value};
    },
    prepare(uid) {
      if (options.auth.currentUser?.uid !== uid) throw new Error('Account changed');
      const bytes = options.crypto.getRandomValues(new Uint8Array(32));
      const value = {uid,requestId:options.crypto.randomUUID(),recoveryKey:Array.from(bytes,b => b.toString(16).padStart(2,'0')).join('')};
      if (!validAttempt(value)) throw new Error('Invalid recovery proof');
      return value;
    },
    async submit(body) {
      if (!saved || saved.requestId !== body.requestId || saved.recoveryKey !== body.recoveryKey) throw new Error('Recovery record must be saved first');
      return call('request',body,true,saved.uid);
    },
    current: () => call('current'),
    status: (requestId,recoveryKey) => call('status',{requestId,recoveryKey},false),
    cancel: body => call('cancel',body),
  };
}
