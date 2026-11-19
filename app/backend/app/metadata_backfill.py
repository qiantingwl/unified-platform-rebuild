"""One-time local metadata backfill. Never contacts source services or changes switches.

Run inside the new unified container with --apply after database backup. The output
contains counts only. Credentials remain encrypted in the existing database.
"""
import argparse,json
from types import SimpleNamespace
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from .models import Account,Credential,Resource
from .native_identity import capture_identity
from .native_lifecycle import control
from .store import Store,uid


def backfill(store,apply=False,batch_size=200):
    cursor='';counts=dict(accounts=0,identity_available=0,identity_updated=0,lifecycle_created=0,credential_unreadable=0)
    while True:
        with store.read() as (s,_):
            ids=list(s.scalars(select(Account.id).where(Account.id>cursor).order_by(Account.id).limit(batch_size)))
        if not ids:break
        with (store.tx() if apply else store.read()) as (s,state):
            for aid in ids:
                current=store.account(s,aid) if apply else s.get(Account,aid)
                account=current if apply else SimpleNamespace(id=current.id,credential_version=current.credential_version,original_label=current.original_label)
                counts['accounts']+=1
                before=account.original_label
                if not before:
                    cred=s.scalar(select(Credential).where(Credential.account_id==aid,Credential.version==account.credential_version))
                    if cred:
                        try:
                            value=json.loads(store.cipher.decrypt(cred.ciphertext.encode()))
                            if isinstance(value,dict):capture_identity(account,value)
                        except (ValueError,TypeError,InvalidToken):counts['credential_unreadable']+=1
                    if account.original_label:counts['identity_updated']+=1
                if account.original_label:counts['identity_available']+=1
                row=s.scalar(select(Resource).where(Resource.kind=='lifecycle_control',Resource.name==aid))
                if row is None:
                    value=control(s,account)
                    counts['lifecycle_created']+=1
                    if apply:s.add(Resource(id=uid(),kind='lifecycle_control',name=aid,data=json.dumps(value),version=1,updated_at=store.clock()))
                if not apply:account.original_label=before
            if apply:store.emit(s,state,'metadata','native_metadata_backfilled')
        cursor=ids[-1]
    return counts


def main():
    import os
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    store=Store(os.environ['UNIFIED_DATABASE_URL'],os.environ['UNIFIED_ENCRYPTION_KEY'].encode())
    try:print(json.dumps({'apply':args.apply,**backfill(store,args.apply)}))
    finally:store.engine.dispose()

if __name__=='__main__':main()
