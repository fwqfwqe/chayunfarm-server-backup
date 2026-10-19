# 恢复说明

适用快照：`20261019-233743`。公开仓库为
[fwqfwqe/chayunfarm-server-backup](https://github.com/fwqfwqe/chayunfarm-server-backup)。

备份包含敏感业务文件、数据库、日志和凭据。GitHub Release 只存放加密后的分片；恢复密钥仅在所有者本机的 `private/restore-key.json` 中，不能提交到仓库或上传到 Release。**遗失密钥将无法恢复。**

## 1. 准备文件并校验

准备此快照对应的 `manifest.json`、全部 Release 分片、恢复脚本及单独保管的密钥。将 `TAG` 替换为实际 Release 标签：

```sh
python -m pip install -r requirements.txt
gh release download TAG -R fwqfwqe/chayunfarm-server-backup -D encrypted
python restore_backup.py --manifest manifest.json --parts-dir encrypted --verify-only
```

分片按清单顺序组成同一个密文流，不要重新压缩、编辑、单独解密或自行改变顺序。缺少任何分片都必须先补齐。

此处只检查密文分片大小和 SHA-256。即使有人同时修改分片及清单，SHA-256 检查也可能通过；后续解密还会检查 AES-GCM 身份认证。

## 2. 使用本机密钥解密

将 `--key` 改为你本机密钥的实际路径。例如，从本地 `repo` 目录使用同级 `private` 目录：

```sh
python restore_backup.py --manifest manifest.json --parts-dir encrypted --key ../private/restore-key.json --output server-backup.tar.gz
```

工具会先检查全部密文分片，再验证 GCM 身份认证、明文大小和 SHA-256。全部成功后，才原子生成 `server-backup.tar.gz`；它拒绝覆盖已有输出。输出文件系统须支持硬链接。失败后按报错检查分片、清单或密钥，不要跳过认证检查。

此命令不会解包、启动容器或执行归档中的内容。解密后的归档包含完整敏感数据，应保存在受控磁盘中。优先在本机解密，再通过受控渠道将归档传到目标主机；不要把恢复密钥保存在公开仓库中。

## 3. 在新 Linux 主机解包到 staging

先准备兼容的 Linux、Docker Engine 和 Docker Compose，确认磁盘空间充足。如目标主机已有同名业务，先备份现有数据，并安排停机切换窗口。

在受控的恢复目录中操作。先查看归档目录结构和文件清单，再解包到独立的 staging 目录，不能直接解包到 `/`：

```sh
tar -tzf server-backup.tar.gz
mkdir -p staging
chmod 700 staging
tar -xzf server-backup.tar.gz -C staging
```

归档顶层固定为 `filesystem/`、`metadata/` 和 `images/`。原服务器路径位于 `filesystem/` 下。以下示例假设当前目录包含 `staging`；业务目录分别为 `staging/filesystem/opt/tea-smart-farm`、`staging/filesystem/srv/tea-smart-farm`，镜像归档为 `staging/images/docker-images.tar`。恢复文件时核对文件所有者、权限、挂载路径和剩余空间。

## 4. 恢复业务文件、数据库和镜像

确认目标业务未运行，且目标目录中没有需要保留的现有数据后，再复制业务目录：

```sh
sudo mkdir -p /opt/tea-smart-farm /srv/tea-smart-farm
sudo cp -a staging/filesystem/opt/tea-smart-farm/. /opt/tea-smart-farm/
sudo cp -a staging/filesystem/srv/tea-smart-farm/. /srv/tea-smart-farm/
sudo docker load -i staging/images/docker-images.tar
```

SQLite 采用在线 backup 生成的一致副本，并执行 `integrity_check`。一致副本直接替换 `filesystem/` 下对应的原数据库文件，旁边的 WAL/SHM 临时文件不进入快照。主数据库预期位于 `staging/filesystem/srv/tea-smart-farm/data/tea-smart-farm.sqlite`，复制业务目录后对应 `/srv/tea-smart-farm/data/tea-smart-farm.sqlite`。

启动应用前，查看 `staging/metadata/snapshot.json` 中每个 SQLite 文件的路径、SHA-256、大小和检查状态；同时可用 `staging/metadata/files-manifest.json` 核对各文件 SHA-256。确认数据库位于部署配置指定的路径，且数据库文件及父目录的所有者、读写权限与容器运行账号匹配。

镜像归档包含当前 3 个容器使用的 3 个 Docker 镜像。将 `docker load` 的导入结果与密文内的镜像、容器清单核对，再确认 Compose 引用的镜像标签或摘要存在。

## 5. 按需恢复系统配置

归档中 `filesystem/` 下的 `/etc`、`/home`、`/root`、`/usr/local`、cron 和云主机配置可供核对、选取和恢复。**不要整体覆盖新机器的 `/etc`、`/home` 或 `/root`。**

根据新主机实际情况逐项处理应用需要的环境变量、服务配置、定时任务、隧道配置、域名、挂载目录和文件权限。保留新主机自己的网络、SSH、用户及云实例初始化设置。Caddy 证书和配置属于敏感内容；先核对域名、权限及使用条件，再按需恢复。不要无条件启动归档中的全部定时任务或系统服务。

容器 inspect、镜像清单和软件包清单等元数据都在密文中，可用于重建运行环境。此备份不包含操作系统系统二进制、虚拟文件系统、未使用的旧 Docker 镜像或缓存，不能作为磁盘镜像直接启动。

## 6. 启动并验证应用

核对恢复后的 Compose 文件、环境变量、数据库路径和持久化卷路径后，执行：

```sh
cd /opt/tea-smart-farm/deploy
sudo docker compose -f compose.yml -f compose.tunnel.yml up -d
sudo docker compose -f compose.yml -f compose.tunnel.yml ps
sudo docker compose -f compose.yml -f compose.tunnel.yml logs --tail=100
```

再检查网站访问、应用登录、核心业务查询、SQLite 数据完整性，以及隧道和证书状态。域名或云实例相关设置需要按目标主机调整。验证完成前保留原始密文分片和本机密钥，不要把解密归档、数据库、生产 `.env` 或检查日志提交到 GitHub。

恢复脚本测试与备份校验只证明相应检查通过，不等于已经完成新主机上的整机重建演练。此次实际验证项目和结果以 Release 发布说明为准。
