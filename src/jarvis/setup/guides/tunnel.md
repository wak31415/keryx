# Phone calls: a tunnel to this machine

Twilio has to reach this machine over HTTPS. A tunnel gives it a public hostname without
opening a port.

**Linux — Cloudflare Tunnel** (needs a domain on Cloudflare):

```bash
cloudflared tunnel login                              # opens a browser once
cloudflared tunnel create jarvis
cloudflared tunnel route dns jarvis jarvis.example.com   # your own hostname
```

`scripts/install-systemd.sh` then runs the tunnel beside Jarvis, and `scripts/dev.sh`
runs both in the foreground. Install `cloudflared` from
<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>.

**macOS — ngrok** with a reserved domain: `brew install ngrok`, sign in
(`ngrok config add-authtoken …`), and reserve a domain at
<https://dashboard.ngrok.com/domains>. `scripts/install-launchd.sh` runs it for you.

Either way, the hostname you give below is `PUBLIC_HOST`: just the name, with no
`https://` and no path.
