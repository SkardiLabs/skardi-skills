---
name: sealos-k8s-deploy
description: Step-by-step workflows for deploying services and static frontends to Sealos via kubectl, including gotchas discovered during hands-on sessions
type: feedback
---

# Sealos Kubernetes Deployment Skill

## 1. Setting up kubectl with Sealos

Download kubeconfig from the Sealos dashboard (Account → kubeconfig), then:

```bash
cp ~/Downloads/kubeconfig.yaml ~/.kube/sealos-config.yaml
export KUBECONFIG=~/.kube/sealos-config.yaml
```

If file not found, ask user to copy-paste the kubeconfig.yaml into the current terminal directory for future processing. 

Your namespace is embedded in the kubeconfig:
```bash
kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}'
# e.g. ns-bg7m761t
```

Always use `KUBECONFIG=~/.kube/sealos-config.yaml kubectl ...` inline or `export KUBECONFIG=...` first — `export` does NOT persist across Bash tool calls.

---

## 2. Deploying a Sealos Template manually

Sealos templates use variables like `${{ defaults.app_name }}`, `${{ SEALOS_CLOUD_DOMAIN }}`, `${{ SEALOS_CERT_SECRET_NAME }}`. When bypassing the template engine, resolve them as:

- **SEALOS_CLOUD_DOMAIN**: infer from existing ingress host suffix
  ```bash
  kubectl get ingress -n <ns> -o jsonpath='{.items[0].spec.rules[0].host}'
  # e.g. tzgwmzo0a.usw-1.sealos.io → domain is usw-1.sealos.io
  ```
- **SEALOS_CERT_SECRET_NAME**: infer from existing ingress TLS
  ```bash
  kubectl get ingress -n <ns> -o jsonpath='{.items[0].spec.tls[0].secretName}'
  # e.g. wildcard-cert
  ```

---

## 3. Ingress domain rules on Sealos (usw-1)

- `*.usw-1.sealos.io` — **forbidden** for user-created ingresses (system only, e.g. terminal)
- `*.usw-1.sealos.app` — **allowed** for user apps; enable a custom subdomain from the Sealos dashboard first
- TLS secret `wildcard-cert` works for both domains — no namespace-local TLS secret needed

---

## 5. Deploying a static frontend via ConfigMaps + nginx

Use `public.ecr.aws/nginx/nginx:alpine` (avoids Docker Hub rate limits).
Mount built files as ConfigMaps with `subPath`:

```yaml
containers:
  - image: public.ecr.aws/nginx/nginx:alpine
    volumeMounts:
      - name: html
        mountPath: /usr/share/nginx/html/index.html
        subPath: index.html
      - name: assets
        mountPath: /usr/share/nginx/html/assets/<hashed>.js
        subPath: <hashed>.js
      - name: nginx-conf
        mountPath: /etc/nginx/conf.d/default.conf
        subPath: default.conf
```

nginx config for a Vite SPA (listens on 8080 for unprivileged):
```nginx
server {
    listen 8080;
    root /usr/share/nginx/html;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
}
```

To update after a new `npm run build` (JS filename hash changes):
```bash
# Update ConfigMap
kubectl create configmap <name>-assets \
  --from-file=<new-hash>.js=dist/assets/<new-hash>.js \
  --from-file=styles.css=dist/assets/styles.css \
  --dry-run=client -o yaml | kubectl apply -f -

# Patch deployment volumeMount to new filename
kubectl patch deployment <name> --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/volumeMounts/1/mountPath","value":"/usr/share/nginx/html/assets/<new-hash>.js"},
       {"op":"replace","path":"/spec/template/spec/containers/0/volumeMounts/1/subPath","value":"<new-hash>.js"}]'
```

---

## 6. Patching a ConfigMap and restarting

```bash
kubectl patch configmap <name> -n <ns> \
  --type=merge \
  -p='{"data":{"file.csv":"line1\nline2\n"}}'

# ConfigMap file mounts are cached — must restart pod to pick up changes
kubectl rollout restart deployment/<name> -n <ns>
```

---

## 7. Skardi-specific patterns

- Endpoint is `POST /<pipeline-name>/execute` with JSON body (not GET with query params)
- All parameters must be present in the body; use `null` for optional filters
- Multiple pipelines: pass a **directory** path to `--pipeline`, not multiple flags
  ```yaml
  args:
    - --pipeline
    - /config/pipelines/   # loads all .yaml files in folder
  ```
- Mount each pipeline YAML into the directory via separate `subPath` volumeMounts
- CORS is `access-control-allow-origin: *` by default — no proxy needed for browser clients

