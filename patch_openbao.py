import yaml
import base64

with open('openbao/config/openbao.hcl', 'rb') as f:
    b64_conf = base64.b64encode(f.read()).decode('utf-8')

with open('docker-compose.yaml', 'r') as f:
    data = yaml.safe_load(f)

openbao = data['services']['openbao']

openbao['entrypoint'] = ['/bin/sh', '-c']
openbao['command'] = f'echo "{b64_conf}" | base64 -d > /tmp/openbao.hcl && exec server -config=/tmp/openbao.hcl'

# Remove the broken bind mount
openbao['volumes'] = [v for v in openbao.get('volumes', []) if 'openbao.hcl' not in v]

with open('docker-compose.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

