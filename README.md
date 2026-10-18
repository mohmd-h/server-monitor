# server-monitor

Very light web panel for a Linux server (Python 3 standard library only, no dependencies).
Shows live CPU, RAM, network speed, total traffic, swap, disk and per-interface counters. Keeps 30 days of history.

## Install / update (run as root)

```
curl -fsSL https://raw.githubusercontent.com/mohmd-h/server-monitor/main/install.sh | sudo bash
```

It installs to `/opt/server-monitor`, starts a systemd service and prints the panel URL
(random port + secret token). Running the same command again updates the panel; history and settings are kept.

If the page does not open from outside, open the printed port in your firewall.

## Show the URL again

```
cat /opt/server-monitor/monitor_config.json
```

The address is `http://SERVER_IP:PORT/TOKEN/`.

## Useful commands

```
systemctl status server-monitor      # is it running?
systemctl restart server-monitor     # restart
journalctl -u server-monitor -n 50   # last log lines
```

## Uninstall

```
systemctl disable --now server-monitor
rm -rf /opt/server-monitor /etc/systemd/system/server-monitor.service
systemctl daemon-reload
```
