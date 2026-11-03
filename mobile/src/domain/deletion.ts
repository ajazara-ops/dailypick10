export const DELETION_DISABLED_MESSAGE = '7일 유예 탈퇴 기능은 운영 연결이 보류되어 있습니다. 이 빌드에서는 탈퇴 신청을 접수하거나 실제 계정을 삭제하지 않습니다.';
export const DELETION_UNVERIFIED_MESSAGE = '인증 또는 네트워크 문제로 처리 상태를 확인하지 못했습니다. 로그아웃이나 계정 조회 실패만으로 삭제 완료를 판단하지 않습니다. 이 연결은 로그인 없이 완료를 확인하는 경로를 제공하지 않습니다.';

export type DeletionTime = {seconds: number; nanoseconds: number};
export function validDeletionTime(value: unknown): value is DeletionTime {
  const t = value as DeletionTime | null;
  return !!t && Number.isSafeInteger(t.seconds) && t.seconds >= 0 && t.seconds <= 253402300799 &&
    Number.isInteger(t.nanoseconds) && t.nanoseconds >= 0 && t.nanoseconds < 1000000000;
}
export const compareDeletionTime = (a: DeletionTime, b: DeletionTime) => a.seconds === b.seconds ? Math.sign(a.nanoseconds-b.nanoseconds) : Math.sign(a.seconds-b.seconds);
export const deletionTimeSeconds = (t: DeletionTime) => t.seconds + t.nanoseconds / 1000000000;
export type DeletionReceipt = {
  version: 1 | 2; requestId: string;
  status: 'grace_period' | 'queued' | 'cancel_requested' | 'cancelled' | 'processing' | 'retry_wait' | 'needs_review' | 'completed';
  phase: 'quiesce' | 'auth' | 'profile' | 'complete';
  requestedAt: number; deleteAfter: number; serverNow: number; cancelAllowed: boolean;
  completedScope: string[]; subscriptionCancelled: false;
  preciseTime?: {requestedAt: DeletionTime; serverNow: DeletionTime};
};
export type DeletionAttempt = {uid: string; requestId: string; recoveryKey: string};
export interface DeletionTransport {
  mode: 'disabled' | 'mock' | 'emulator' | 'live'; // App bootstrap stays disabled; prepared live binding requires explicit opt-in.
  supportsRecoveryStatus?: boolean; // Explicitly verified mock/local proof path only; default is authenticated-only.
  subscribeChanges?(listener: () => void): () => void;
  load(): Promise<DeletionAttempt | null>;
  save(attempt: DeletionAttempt): Promise<void>;
  prepare(uid: string): DeletionAttempt;
  submit(body: {version: 1; requestId: string; recoveryKey: string; confirmation: 'DELETE_MY_ACCOUNT'}): Promise<unknown>;
  status(requestId: string, recoveryKey: string): Promise<unknown | null>;
  current(): Promise<unknown | null>;
  cancel(body: {version: 1; requestId: string; confirmation: 'CANCEL_ACCOUNT_DELETION'}): Promise<unknown>;
}
export type DeletionState = {
  mode: DeletionTransport['mode'];
  status: DeletionReceipt['status'] | 'disabled' | 'loading' | 'idle' | 'reauthenticating' | 'submitting' | 'unknown' | 'not_found';
  message?: string; receipt?: DeletionReceipt;
};
const idPattern = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
export function validAttempt(value: unknown): value is DeletionAttempt {
  if (!value || typeof value !== 'object') return false;
  const v = value as DeletionAttempt;
  return typeof v.uid === 'string' && !!v.uid && !v.uid.includes('/') && v.uid.length <= 128 &&
    typeof v.requestId === 'string' && idPattern.test(v.requestId) && typeof v.recoveryKey === 'string' && /^[a-f0-9]{64}$/.test(v.recoveryKey);
}
export function parseDeletionReceipt(value: unknown, requestId: string): DeletionReceipt {
  if (!value || typeof value !== 'object') throw new Error('Invalid receipt');
  const v = value as DeletionReceipt;
  const phases = ['quiesce', 'auth', 'profile', 'complete'];
  if (![1,2].includes(v.version) || v.requestId !== requestId || v.subscriptionCancelled !== false ||
      !['grace_period', 'queued', 'cancel_requested', 'cancelled', 'processing', 'retry_wait', 'needs_review', 'completed'].includes(v.status) || !phases.includes(v.phase) ||
      !Array.isArray(v.completedScope)) throw new Error('Invalid receipt');
  let beforeDeadline: boolean;
  if (v.version === 2) {
    const times = v.preciseTime;
    if (!times || !validDeletionTime(times.requestedAt) || !validDeletionTime(times.serverNow) ||
        compareDeletionTime(times.serverNow, times.requestedAt) < 0 ||
        v.requestedAt !== deletionTimeSeconds(times.requestedAt) || v.serverNow !== deletionTimeSeconds(times.serverNow) ||
        v.deleteAfter !== v.requestedAt + 604800) throw new Error('Invalid precise server deadline');
    beforeDeadline = compareDeletionTime(times.serverNow, {...times.requestedAt, seconds: times.requestedAt.seconds + 604800}) < 0;
  } else {
    if (![v.requestedAt,v.deleteAfter,v.serverNow].every(n => Number.isSafeInteger(n) && n >= 0 && n <= 8640000000000) || v.deleteAfter-v.requestedAt !== 604800 || v.serverNow < v.requestedAt) throw new Error('Invalid server deadline');
    beforeDeadline = v.serverNow < v.deleteAfter;
  }
  if (typeof v.cancelAllowed !== 'boolean') throw new Error('Invalid server deadline');
  if (v.cancelAllowed !== (v.status === 'grace_period' && beforeDeadline) || (v.status === 'grace_period' && !beforeDeadline) || (v.status === 'queued' && beforeDeadline)) throw new Error('Invalid cancellation window');
  if (['queued','cancel_requested','cancelled','grace_period'].includes(v.status) && v.phase !== 'quiesce') throw new Error('Invalid phase');
  if (v.status === 'completed') {
    if (beforeDeadline) throw new Error('Completion precedes deletion deadline');
    if (v.phase !== 'complete' || v.completedScope.length !== 2 || !v.completedScope.includes('firebase-auth') || !v.completedScope.includes('users-document')) throw new Error('Unconfirmed completion');
  } else if (v.phase === 'complete' || v.completedScope.length !== 0) throw new Error('Inconsistent receipt');
  return v;
}

export function createDeletionClient(transport: DeletionTransport, currentUid: () => string | null) {
  let state: DeletionState = {mode: transport.mode, status: transport.mode === 'disabled' ? 'disabled' : 'loading'};
  let attempt: DeletionAttempt | null = null, owner: string | null = null, busy = false;
  let initialized = transport.mode === 'disabled';
  const listeners = new Set<(value: DeletionState) => void>();
  const emit = (status: DeletionState['status'], message?: string, receipt?: DeletionReceipt) => {
    state = {mode: transport.mode, status, message, receipt}; listeners.forEach(listener => listener(state));
  };
  const apply = (value: unknown, expectedId?: string) => {
    const id = expectedId ?? (value as {requestId?: string})?.requestId;
    if (typeof id !== 'string' || !idPattern.test(id)) throw new Error('Invalid receipt identifier');
    const parsed = parseDeletionReceipt(value, id); emit(parsed.status, undefined, parsed);
  };
  const refresh = async () => {
    if (transport.mode === 'disabled' || !initialized || busy) return;
    busy = true;
    const uid = currentUid();
    try {
      if (uid) {
        let result: unknown | null;
        let expectedId: string | undefined;
        try { result = await transport.current(); }
        catch (error) {
          // Admin deletion does not immediately clear Firebase's cached currentUser.
          // A saved receipt can still confirm cleanup without the now-invalid Auth token.
          if (transport.supportsRecoveryStatus !== true || attempt?.uid !== uid || currentUid() !== uid) throw error;
          expectedId = attempt.requestId;
          result = await transport.status(attempt.requestId, attempt.recoveryKey);
        }
        if (currentUid() !== uid) { owner = null; emit('unknown'); return; }
        owner = uid;
        if (result !== null) {
          // A cancelled old receipt cannot prove there is no newer request on another device.
          if (expectedId && parseDeletionReceipt(result, expectedId).status === 'cancelled') emit('unknown');
          else apply(result, expectedId);
        }
        else emit(attempt?.uid === uid ? 'not_found' : 'idle');
      } else if (attempt) {
        owner = attempt.uid;
        if (transport.supportsRecoveryStatus !== true) throw new Error('Authenticated status unavailable');
        const result = await transport.status(attempt.requestId, attempt.recoveryKey);
        if (currentUid() !== null) { owner = null; emit('unknown'); return; }
        if (result === null) emit('not_found'); else apply(result, attempt.requestId);
      } else {owner = null; emit('idle');}
    } catch {owner = uid ?? attempt?.uid ?? null; emit('unknown', transport.supportsRecoveryStatus === true ? undefined : DELETION_UNVERIFIED_MESSAGE);}
    finally {busy = false;}
  };
  return {
    snapshot: () => state,
    subscribe(listener: (value: DeletionState) => void) {listeners.add(listener); return () => {listeners.delete(listener);};},
    canUseService(uid: string | null | undefined) {
      return !!uid && (transport.mode === 'disabled' || (initialized && !busy && owner === uid && ['idle','cancelled'].includes(state.status)));
    },
    async initialize() {
      if (initialized) return;
      try {
        const saved = await transport.load();
        if (saved !== null && !validAttempt(saved)) throw new Error('Invalid recovery record');
        attempt = saved; initialized = true; await refresh();
      } catch {emit('unknown', '복구 기록을 읽지 못했습니다. 앱을 다시 시작해주세요.');}
    },
    refresh,
    async request(reauthenticate: () => Promise<boolean>) {
      if (transport.mode === 'disabled' || busy || !initialized) return;
      const uid = currentUid(); if (!uid) return;
      if (owner === uid && !['idle','cancelled','unknown','not_found'].includes(state.status)) {await refresh(); return;}
      if (attempt && attempt.uid !== uid) {emit('unknown', '다른 계정의 처리 기록을 먼저 확인해주세요.'); return;}
      const previous = state;
      busy = true; emit('reauthenticating');
      try {
        if (!await reauthenticate()) {emit(previous.status, undefined, previous.receipt); return;}
        if (currentUid() !== uid) {owner=null; emit('unknown', '계정이 바뀌어 요청을 중단했습니다.'); return;}
        if (!attempt || previous.status === 'cancelled') {
          const prepared = transport.prepare(uid);
          if (!validAttempt(prepared) || prepared.uid !== uid) throw new Error('Invalid recovery proof');
          await transport.save(prepared); attempt = prepared;
        }
        owner = uid;
        if (currentUid() !== uid) {emit('unknown'); return;}
        emit('submitting');
        const result = await transport.submit({version:1,requestId:attempt.requestId,recoveryKey:attempt.recoveryKey,confirmation:'DELETE_MY_ACCOUNT'});
        if (currentUid() !== uid) { owner=null; emit('unknown'); return; }
        apply(result, attempt.requestId);
      } catch {emit(attempt ? 'unknown' : previous.status, attempt ? undefined : '요청 준비 또는 본인 확인에 실패했습니다. 삭제 요청을 보내지 않았습니다.', previous.receipt);}
      finally {busy=false;}
    },
    async cancel(reauthenticate: () => Promise<boolean>) {
      if (transport.mode === 'disabled' || busy || !initialized || state.status !== 'grace_period' || !state.receipt?.cancelAllowed) return;
      const uid=currentUid(), previous=state;
      if (!uid || uid !== owner) return;
      busy=true; emit('reauthenticating');
      try {
        if (!await reauthenticate()) {emit(previous.status,undefined,previous.receipt);return;}
        if (currentUid() !== uid) {owner=null;emit('unknown');return;}
        const result = await transport.cancel({version:1,requestId:previous.receipt!.requestId,confirmation:'CANCEL_ACCOUNT_DELETION'});
        if (currentUid() !== uid) { owner=null; emit('unknown'); return; }
        apply(result, previous.receipt!.requestId);
      } catch {emit('unknown','취소 완료를 확인하지 못했습니다. 서버 상태를 다시 확인해주세요.');}
      finally {busy=false;}
    },
  };
}

