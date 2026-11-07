"""Synthetic adapters only. No SDKs, external requests or credential loading."""
from account_deletion import Principal, Unauthorized, IdentityChanged, TemporaryFailure, PermissionFailure, push_eligible


class MemoryAuth:
    synthetic_only = True
    def __init__(self):
        self.users, self.tokens, self.failures, self.calls = {}, {}, {}, []
        self.valid_after, self.disabled = {}, set()

    def seed(self, uid, now, *, created_at=1, auth_time=None):
        self.users[uid] = created_at
        token = 'synthetic-token:'+uid
        self.tokens[token] = Principal(uid, now if auth_time is None else auth_time, now+3600, created_at)
        return token

    def verify(self, token):
        principal = self.tokens.get(token)
        if not principal or self.users.get(principal.uid) != principal.created_at:
            raise Unauthorized('Invalid synthetic authentication')
        return principal

    def delete_if_same(self, uid, generation):
        self.calls.append(uid)
        if self.failures.get(uid) == 'before': raise TemporaryFailure('Synthetic Auth failure')
        if uid in self.users and self.users[uid] != generation: raise IdentityChanged('UID generation changed')
        self.users.pop(uid, None)
        if self.failures.get(uid) == 'after': raise TemporaryFailure('Synthetic lost Auth response')

    def ensure_same_or_absent(self, uid, generation):
        if self.failures.get(uid) == 'lookup': raise TemporaryFailure('Synthetic Auth lookup failure')
        if uid in self.users and self.users[uid] != generation: raise IdentityChanged('UID generation changed')

    def verify_recorded_consent(self, uid, generation, auth_time):
        self.ensure_same_or_absent(uid, generation)
        if uid not in self.users: return
        if type(auth_time) is not int or uid in self.disabled or auth_time * 1000 < self.valid_after.get(uid, 0):
            raise PermissionFailure('Synthetic consent revoked or disabled')


class MemoryProfiles:
    synthetic_only = True
    def __init__(self, store):
        self.store, self.documents, self.unscoped, self.failures, self.calls = store, {}, {}, {}, []

    def write_as_client(self, uid, data):
        if self.store.blocked(uid): raise Unauthorized('Deletion tombstone blocks even a stale session')
        self.documents[uid] = data

    def quiesce(self, uid):
        if self.failures.get(uid) == 'quiesce': raise TemporaryFailure('Synthetic quiesce failure')
        if uid in self.documents: self.documents[uid]['deletionRequest'] = {'status':'pending'}

    def delete_user_document(self, uid):
        self.calls.append(uid)
        if self.failures.get(uid) == 'before': raise TemporaryFailure('Synthetic profile failure')
        self.documents.pop(uid, None)
        if self.failures.get(uid) == 'after': raise TemporaryFailure('Synthetic lost profile response')

    def recipients(self):
        return [uid for uid, data in self.documents.items() if push_eligible(data, self.store.blocked(uid))]
