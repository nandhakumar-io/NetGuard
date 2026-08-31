import yaml

with open('docker-compose.yaml', 'r') as f:
    data = yaml.safe_load(f)

openbao = data['services']['openbao']

# Fix the command so that it delegates to docker-entrypoint.sh or the native bao/vault binary
old_cmd = openbao['command']
if isinstance(old_cmd, list) and old_cmd and old_cmd[0].startswith('echo'):
    new_cmd = old_cmd[0].replace(
        'exec server -config=/tmp/openbao.hcl', 
        'exec docker-entrypoint.sh server -config=/tmp/openbao.hcl || exec bao server -config=/tmp/openbao.hcl || exec vault server -config=/tmp/openbao.hcl'
    )
    openbao['command'] = [new_cmd]

with open('docker-compose.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

