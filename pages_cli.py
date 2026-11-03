"""Workflow continuation CLI. Never starts a scan or submits a Pages deployment."""
import argparse
import json
import os
from pathlib import Path

from pages_delivery import PagesDelivery, PublicJSON, configuration
from publication_runtime import build_daily_publisher


def build_delivery(*, environment=None, request=None, post=None, get=None, scanner=None, asset_policy=None):
    e = os.environ if environment is None else environment
    # Existing factory checks all gates before any credentials/HTTP/Firestore access.
    if e.get('DAILYPICK_GUARDED_RUNTIME') != 'enabled-v1':
        raise RuntimeError('Guarded publication is disabled')
    configuration(e)
    if scanner is None:
        import daily_stock_scanner as scanner
    publisher, _ = build_daily_publisher(scanner, environment=e, request=request, post=post)
    config = configuration(e)
    if get is None:
        import requests
        get = requests.get
    return PagesDelivery(publisher, config, PublicJSON(config['baseUrl'], get),asset_policy=asset_policy)


def main(argv=None, *, factory=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'resume', 'stage', 'archive-check', 'claim', 'verify'])
    parser.add_argument('--result')
    parser.add_argument('--handoff', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--artifact-id')
    parser.add_argument('--delivery-id')
    parser.add_argument('--output', help='Explicit local GitHub step output file')
    args = parser.parse_args(argv)
    delivery = (factory or build_delivery)()
    if args.command in {'prepare', 'resume'}:
        if args.command == 'resume':
            with delivery.repository.publisher_lock(delivery.run_id):
                _, _, row = delivery._load(args.delivery_id)
                keys = row['keys']
        else:
            result = json.loads(Path(args.result).read_text('utf-8'))
            rows = list(result['results'].values()) if result.get('state') == 'markets' else [result]
            if any(r.get('state') not in {'published','noop','holiday'} for r in rows):
                raise RuntimeError('collection_result_unconfirmed')
            keys = [r['key'] for r in rows if r.get('state') != 'holiday']
        prepared = delivery.prepare(keys)
        Path(args.handoff).write_text(json.dumps(prepared), encoding='utf-8')
        output = {'action': prepared['action']}
        if prepared.get('handoff'):
            handoff = prepared['handoff']
            output.update(publication_sha=handoff['publicationSha'],
                          artifact_name='pages-' + handoff['attempt']['id'])
        if args.output:
            # All values are generated enums/hex, never response bodies or credentials.
            with Path(args.output).open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(''.join(k+'='+v+'\n' for k,v in output.items()))
        return output
    prepared = json.loads(Path(args.handoff).read_text('utf-8'))
    handoff = prepared['handoff']
    if args.command == 'stage':
        return delivery.stage(handoff, args.archive)
    if args.command == 'archive-check':
        return delivery.check_archive(handoff,args.archive)
    if args.command == 'claim':
        return delivery.claim(handoff, args.artifact_id,args.archive)
    result = delivery.verify(handoff['id'])
    if result['state'] != 'verified':
        raise RuntimeError('Pages delivery pending: ' + result['reason'])
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception:
        # Do not dump third-party exceptions, env, credentials or response bodies.
        print('Pages continuation incomplete; inspect bounded ledger observation. No automatic resend.')
        raise SystemExit(1)
