import yaml
with open('docker-compose.yaml', 'r') as f:
    data = yaml.safe_load(f)

for db_name in ['db', 'keycloak-db']:
    if db_name in data['services']:
        db = data['services'][db_name]
        caps = db.get('cap_add', [])
        for cap in ['CHOWN', 'FOWNER', 'SETGID', 'SETUID', 'DAC_OVERRIDE']:
            if cap not in caps:
                caps.append(cap)
        db['cap_add'] = caps

with open('docker-compose.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

