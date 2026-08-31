import yaml
with open('docker-compose.yaml', 'r') as f:
    data = yaml.safe_load(f)

for db_name in ['db', 'keycloak-db']:
    if db_name in data['services']:
        db = data['services'][db_name]
        tmpfs = db.get('tmpfs', [])
        if isinstance(tmpfs, list):
            if '/var/run/postgresql' not in tmpfs:
                tmpfs.append('/var/run/postgresql')
            db['tmpfs'] = tmpfs

with open('docker-compose.yaml', 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

