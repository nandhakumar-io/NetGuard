import yaml

with open('docker-compose.yaml', 'r') as f:
    data = yaml.safe_load(f)

for svc_name in ['redis', 'nats', 'keycloak', 'pgbouncer']:
    if svc_name in data['services']:
        svc = data['services'][svc_name]
        caps = svc.get('cap_add', [])
        if isinstance(caps, list):
            for cap in ['SETUID', 'SETGID', 'CHOWN']:
                if cap not in caps:
                    caps.append(cap)
            svc['cap_add'] = caps

with open('docker-compose.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

