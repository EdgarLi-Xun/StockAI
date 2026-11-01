# 内网和反向代理

局域网默认关闭。请先在本机配置密码、私有网段与 LAN 开关，保存并重启；服务才会从回环监听改为 0.0.0.0。Windows 防火墙需由用户自行仅向所需私有来源放行 TCP 8765。程序不会自动放开系统防火墙。

直接内网访问使用 `http://<Windows内网IP>:8765`。登录角色可选管理员与只读；只读用户没有写接口与备份权限。建议经过已有的内网 HTTPS 反向代理访问。

代理位于同一主机时，设置中的 `proxy_hostname` 可配置明确的域名，例如 `research.home.arpa`。该字段也支持配置 JSON 导入。不得配置公共未知来源。示例 Nginx location：

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 650s;
}
```

自行配置内网域名解析和受信任 TLS 证书。本机代理的请求带转发头后，应用会核验原始私有 IP 并强制登录，不把回环代理视为本机管理员。LAN 关闭时任何代理请求都被拒绝。不信任任意远端代理头，不使用 Uvicorn 的自动代理信任配置。

不代理或开放 11434，模型请求仍由后端本机完成。当前验收覆盖 API 级源网段、认证、只读权限、跨站防护与代理绕过测试；第二台物理设备、实际路由和 TLS 部署需在目标网络验证。
