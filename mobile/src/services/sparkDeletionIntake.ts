// Auth/Firestore intake; localhost integration plus an explicitly disabled prepared binding.
// The application's default accountDeletionTransport remains disabled.
import { type Auth } from 'firebase/auth';
import { type Firestore, doc, getDoc, runTransaction, serverTimestamp } from 'firebase/firestore/lite';

export function createLocalSparkIntake(auth: Auth, db: Firestore) {
  if (auth.app.options.projectId !== 'demo-dailypick10-deletion' || db.app !== auth.app
      || auth.emulatorConfig?.host !== '127.0.0.1' || auth.emulatorConfig?.port !== 19099) {
    throw new Error('Only the explicit local demo Auth/Firestore binding is allowed');
  }
  // Firestore's public type does not expose emulator configuration. Verify its
  // already-configured endpoint before ANY SDK operation; never configure a live app.
  const settings = (db as unknown as { _settings: { host: string; ssl: boolean } })._settings;
  if (settings.host !== '127.0.0.1:18080' || settings.ssl !== false) throw new Error('Exact local Firestore required');
  return buildSparkIntake(auth,db);
}

// Prepared binding only; not selected by app bootstrap or environment variables.
// Callers must explicitly opt in after deployment approval and bind both existing SDKs.
export function createPreparedSparkIntake(auth: Auth, db: Firestore, expectedProjectId: string, enabled = false) {
  if (enabled !== true) throw new Error('Operational intake is disabled');
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(expectedProjectId) || expectedProjectId.startsWith('demo-') ||
      auth.app.options.projectId !== expectedProjectId || db.app !== auth.app || auth.emulatorConfig) throw new Error('Approved project binding required');
  const settings=(db as unknown as {_settings:{host:string;ssl:boolean}})._settings;
  if(settings.host!=='firestore.googleapis.com'||settings.ssl!==true)throw new Error('Exact authenticated Firestore endpoint required');
  const database=(db as unknown as {_databaseId:{projectId:string;database:string}})._databaseId;
  if(database?.projectId!==expectedProjectId||database?.database!=='(default)')throw new Error('Exact default database required');
  return buildSparkIntake(auth,db);
}

function buildSparkIntake(auth: Auth, db: Firestore) {
  const identity = () => {
    const user = auth.currentUser;
    if (!user) throw new Error('Authentication required');
    return user;
  };
  const consent = async (user: ReturnType<typeof identity>) => {
    // Refresh can reject revoked sessions. Rules still bind this value to the
    // token; the trusted worker must independently check current Auth state.
    const token = await user.getIdTokenResult(true);
    const authTime = token.claims.auth_time;
    if (auth.currentUser !== user || !Number.isSafeInteger(authTime) || Number(authTime) < 0) throw new Error('Invalid authenticated consent');
    return Number(authTime);
  };
  const current = async () => {
    const user = identity();
    const snapshot = await getDoc(doc(db, 'accountDeletionIntents', user.uid));
    if (auth.currentUser !== user) throw new Error('Account changed');
    return snapshot.exists() ? snapshot.data() : null;
  };
  return {
    current,
    async request(requestId: string, confirmation: string) {
      if (confirmation !== 'DELETE_MY_ACCOUNT' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(requestId)) throw new Error('Explicit new request required');
      const user = identity(), state = doc(db, 'accountDeletionIntents', user.uid);
      const consentAuthTime = await consent(user);
      await runTransaction(db, async tx => {
        const prior = await tx.get(state);
        if (auth.currentUser !== user) throw new Error('Account changed');
        if (prior.exists() && prior.data().status !== 'cancelled') return; // Never extend an active deadline.
        const history = doc(state, 'attempts', requestId), used = await tx.get(history);
        if (used.exists()) throw new Error('Previous request cannot be replayed; confirm with a new identifier');
        const profile = doc(db, 'users', user.uid), existing = await tx.get(profile);
        if (auth.currentUser !== user) throw new Error('Account changed');
        tx.set(state, {version:2, requestId, requestedAt:serverTimestamp(), status:'grace_period', confirmation,
          consentAuthTime, profilePresent:existing.exists(), cancelledAt:null});
        tx.set(history, {requestedAt:serverTimestamp(), consentAuthTime});
        if (existing.exists()) tx.update(profile, {deletionRequest:{requestId}});
      });
      return current(); // Server-acknowledged timestamp; local clock is never persisted.
    },
    async cancel(requestId: string, confirmation: string) {
      if (confirmation !== 'CANCEL_ACCOUNT_DELETION') throw new Error('Explicit cancellation required');
      const user = identity(), state = doc(db, 'accountDeletionIntents', user.uid);
      const cancelAuthTime = await consent(user);
      await runTransaction(db, async tx => {
        const snapshot = await tx.get(state);
        if (auth.currentUser !== user || !snapshot.exists() || snapshot.data().requestId !== requestId) throw new Error('Wrong current request');
        const value = snapshot.data();
        if (value.status === 'cancelled' || value.status === 'cancel_requested') return;
        if (value.status !== 'grace_period') throw new Error('Cancellation closed');
        tx.update(state, {status:'cancel_requested', cancelRequestedAt:serverTimestamp(), cancelAuthTime, confirmation});
        // Only a trusted server confirmation may remove the service/push block.
        // Intake must also work if an administrator has removed the profile.
      });
      return current(); // Rules, not the device clock, determine the seven-day cutoff.
    },
  };
}
