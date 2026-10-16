# Batfish

Batfish reads configs in `configs/`.
It catches routing loops, typos, and bad references before provisioning.

**Start:** (Proxmox/Docker):

```bash
docker run -d --name batfish --restart unless-stopped -p 9996:9996 batfish/batfish:2026.08.27.3685
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