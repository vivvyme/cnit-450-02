# Batfish

Batfish reads configs in `configs/`. 
It will route incorrect loops, typos, and  references before provisioning.

**Start:** (Proxmox/Docker):

```bash
docker run -d --name batfish -p 9996:9996 -p 9997:9997 batfish/allinone
```

**Analysis:**

```bash
python scripts/analyze_configs.py --host <batfish-vm-ip>
```

**Push:**

Run `push_configs.py` to deploy.

**To-Do:**
- Report-only is set by default.
- Some scripts in `configs/` are not running-configs and need to be turned into command scripts to parse.