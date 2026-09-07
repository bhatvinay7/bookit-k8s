#!/usr/bin/env python3
"""Import delivery credentials into local deployment env files without printing them."""
import argparse
from pathlib import Path
import re
import shlex
import json
import os
import subprocess
import tempfile

K8S_ROOT = Path(__file__).resolve().parents[1]
ROOT = K8S_ROOT.parent


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            parts = shlex.split(match[2], comments=True)
            values[match[1]] = ' '.join(parts)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'apps/http-server/.env')
    parser.add_argument('--seal', nargs=3, metavar=('ENVIRONMENT', 'REGION', 'CONTEXT'),
                        help='merge delivery keys into existing regional backend SealedSecret')
    parser.add_argument('--kubeseal', default='kubeseal')
    args = parser.parse_args()
    try:
        source = read_env(args.source)
    except (OSError, ValueError):
        parser.error('cannot read source env file or invalid dotenv quoting')
    keys = ['CLOUDFLARE_R2_ACCOUNT_ID', 'CLOUDFLARE_R2_ACCESS_KEY_ID',
            'CLOUDFLARE_R2_SECRET_ACCESS_KEY', 'CLOUDFLARE_R2_BUCKET',
            'CLOUDFLARE_R2_PUBLIC_URL', 'GMAIL_USER', 'GMAIL_APP_PASSWORD']
    for key in keys:
        if not source.get(key):
            parser.error(f'{key} is missing from source')
    account = source['CLOUDFLARE_R2_ACCOUNT_ID']
    if not re.fullmatch(r'[a-fA-F0-9]{32}', account):
        parser.error('CLOUDFLARE_R2_ACCOUNT_ID must contain 32 hexadecimal characters')
    values = {key: source[key] for key in keys}
    values['CLOUDFLARE_R2_ENDPOINT'] = f'https://{account}.r2.cloudflarestorage.com'
    values.update(SMTP_HOST=source.get('SMTP_HOST') or 'smtp.gmail.com',
                  SMTP_PORT=source.get('SMTP_PORT') or '465',
                  SMTP_SECURE=source.get('SMTP_SECURE') or ('true' if source.get('SMTP_PORT', '465') == '465' else 'false'),
                  SMTP_USER=source.get('SMTP_USER') or source['GMAIL_USER'],
                  SMTP_PASS=source.get('SMTP_PASS') or source['GMAIL_APP_PASSWORD'],
                  SMTP_FROM=source.get('SMTP_FROM') or source['GMAIL_USER'])
    if values['SMTP_SECURE'] not in ('true', 'false'):
        parser.error('SMTP_SECURE must be true or false')
    if (values['SMTP_PORT'], values['SMTP_SECURE']) in [('465', 'false'), ('587', 'true')]:
        parser.error('SMTP TLS mismatch: use 465/true or 587/false')
    if not values['SMTP_PORT'].isdigit() or not 0 < int(values['SMTP_PORT']) <= 65535:
        parser.error('SMTP_PORT must be a valid port')
    for target in [ROOT / '.env.deployment', K8S_ROOT / '.env.deployment']:
        lines = target.read_text().splitlines() if target.exists() else []
        remaining = dict(values)
        for index, line in enumerate(lines):
            key = line.split('=', 1)[0]
            if key in remaining:
                lines[index] = f'{key}={shlex.quote(remaining.pop(key))}'
        lines.extend(f'{key}={shlex.quote(value)}' for key, value in remaining.items())
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
        target.write_text('\n'.join(lines) + '\n')
        print(f'Updated {target.relative_to(ROOT)} (values hidden)')
    if args.seal:
        seal_delivery(parser, args, values)


def seal_delivery(parser, args, values):
    environment, region, context = args.seal
    if environment not in ('dev', 'prod') or not re.fullmatch(r'[a-z0-9-]+', region):
        parser.error('invalid environment or region')
    target = K8S_ROOT / f'apps/regions/{environment}/{region}/secrets/sealed-backend-secrets.yaml'
    if not target.exists():
        parser.error('regional backend SealedSecret is missing; run seal-cluster-secrets.sh first to create the complete cluster secrets')
    secret = {'apiVersion': 'v1', 'kind': 'Secret',
              'metadata': {'name': 'backend-secrets', 'namespace': 'bookit'},
              'type': 'Opaque', 'stringData': values}
    # Merge into a temporary copy so a failed seal cannot truncate existing ciphertext.
    with tempfile.TemporaryDirectory() as directory:
        merged = Path(directory) / 'sealed-backend-secrets.yaml'
        merged.write_bytes(target.read_bytes())
        try:
            result = subprocess.run([
            args.kubeseal, '--context', context,
            '--controller-name', os.environ.get('SEALED_SECRETS_CONTROLLER_NAME', 'sealed-secrets'),
            '--controller-namespace', 'kube-system', '--format', 'yaml',
            '--merge-into', str(merged),
            ], input=json.dumps(secret), text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            parser.error('kubeseal is unavailable or timed out; verify its installation and cluster access')
        if result.returncode:
            parser.error('kubeseal failed; verify the context and Sealed Secrets controller (tool output hidden to protect credentials)')
        target.write_bytes(merged.read_bytes())
    print(f'Updated encrypted delivery settings in {target.relative_to(K8S_ROOT)}')


if __name__ == '__main__':
    main()
