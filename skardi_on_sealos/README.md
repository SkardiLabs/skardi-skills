# Skardi on Sealos

Before using this skill, make sure you have the following ready:

## 1. A Free Sealos Account

Sign up at [sealos.io](https://sealos.io) to get a free account. Sealos provides a cloud platform for deploying containerized apps with Kubernetes under the hood.

## 2. The Sealos Config YAML File

You'll need access to the Sealos kubeconfig YAML file to authenticate and deploy to your Sealos cluster. This is typically available from your Sealos dashboard under account settings. Keep this file handy — the skill will reference it during deployment.

## 3. Docker Installed and Logged In Locally

Make sure Docker is installed and you are logged into your Docker account:

```bash
docker login
```

You'll need a Docker Hub account (or another container registry) to push the built image before Sealos can pull and deploy it.

---

Once all three are in place, you're ready to run the skill!
