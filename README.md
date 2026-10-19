# 茶韵智农服务器加密备份

这是应服务器所有者要求建立的**公开 GitHub 仓库**：
[fwqfwqe/chayunfarm-server-backup](https://github.com/fwqfwqe/chayunfarm-server-backup)。
本次快照编号为 `20261019-233743`。

仓库公开恢复工具、测试、依赖说明和 `manifest.json` 校验清单。服务器源码、数据库、日志、证书、账号及其他凭据的明文均不公开。完整备份经过 AES-256-GCM 加密后，按顺序切为每片不超过 100 MiB 的原始分片，存放在本仓库的 GitHub Release 中。

恢复密钥只保存在所有者本机备份目录的 `private/restore-key.json`，位于此 Git 仓库之外。**绝不能提交或上传密钥、解密后的归档、原始数据及生产配置。密钥遗失后无法解密备份。** 请另外妥善保管密钥的离线副本；GitHub 上的公开文件不能代替密钥。

## 备份范围

备份包含以下业务文件和配置，具体文件以加密归档内的清单为准：

- `/opt/tea-smart-farm`，包括 `app`、`deploy`、`backups` 和 `releases`。
- `/srv/tea-smart-farm`，包括 SQLite 数据及 Caddy 证书、配置。
- `/etc`、`/home`、`/root`、`/usr/local` 和 `/var/log`。
- cron、云主机配置，以及当前 3 个容器使用的 3 个 Docker 镜像。
- 容器 inspect 结果、镜像及软件包清单等恢复元数据。

SQLite 通过在线 backup 生成一致副本，并执行 `integrity_check`；一致副本直接放回归档中对应的数据库路径，旁边的 WAL/SHM 临时文件不进入快照。上述业务数据和元数据均保存在密文中。

归档顶层为 `filesystem/`、`metadata/` 和 `images/`。原服务器路径保存在 `filesystem/` 下；`metadata/snapshot.json` 记录 SQLite 路径、SHA-256、大小和检查状态，`metadata/files-manifest.json` 记录各文件的 SHA-256，`images/docker-images.tar` 保存 Docker 镜像。

此备份排除操作系统系统二进制、虚拟文件系统、未使用的旧 Docker 镜像和缓存，**不是整块磁盘镜像**。恢复需要准备兼容的 Linux、Docker 与 Docker Compose 环境，并根据新机器情况恢复配置。

## 下载与校验

先安装 Python 依赖：

```sh
python -m pip install -r requirements.txt
```

使用固定标签 `snapshot-20261019-233743`，下载全部附件到 `encrypted` 目录。使用此快照对应的 `manifest.json`，不要混用其他快照的清单或分片。

```sh
gh release download snapshot-20261019-233743 -R fwqfwqe/chayunfarm-server-backup -D encrypted
python restore_backup.py --manifest manifest.json --parts-dir encrypted --verify-only
```

`--verify-only` 只验证全部密文分片的大小和 SHA-256，不需要密钥，也不会验证 GCM 身份认证或检查明文内容。SHA-256 校验通过不等于备份已经成功解密。

## 解密与恢复

密钥路径可自行指定。若在本机的 `repo` 目录运行，且密钥位于同级 `private` 目录，可使用：

```sh
python restore_backup.py --manifest manifest.json --parts-dir encrypted --key ../private/restore-key.json --output server-backup.tar.gz
```

恢复工具先验证所有密文分片，再流式解密；只有 GCM 身份认证、明文大小和 SHA-256 全部通过后，才生成指定的输出文件。已有文件不会被覆盖，失败时会清理本次创建的临时文件。输出所在文件系统必须支持硬链接，以便原子且不覆盖地生成最终文件。

工具只生成归档，不会自动解包或执行服务器命令。解密后的 `server-backup.tar.gz` 包含敏感数据，请仅保存在受控位置。具体恢复步骤见 [RESTORE.md](RESTORE.md)。

## 格式与工具检查

密文是一个连续 AES-256-GCM 流：`CHAYUNBACKUP` 加单字节版本号 `0x01`、12 字节 nonce、密文、16 字节认证 tag。完整头部作为 AAD；各分片不单独加密，必须按 `manifest.json` 中的顺序读取。

恢复工具的合成数据测试可在本地运行；测试不会读取生产密钥或实际服务器数据：

```sh
python -m unittest -v test_restore_backup.py
```

Release 附件、最终校验结果及验证范围以本快照的发布说明为准。本说明不声称已完成新机器上的整机重建演练。

## 本次快照验证结果

- 业务快照时间：2026-10-19 23:43:55（Asia/Shanghai）。
- 压缩归档：507,788,570 字节；加密后共 5 个分片、507,788,611 字节。
- 真实备份已解密并通过 GCM、归档 SHA-256、12,953 个文件 SHA-256、14 个 SQLite integrity_check 和 3 个 Docker 镜像清单检查。
- [验证摘要](verification.json)；[加密分片下载](https://github.com/fwqfwqe/chayunfarm-server-backup/releases/tag/snapshot-20261019-233743)。
- 验证覆盖归档内容和数据库完整性，没有在新主机启动整个业务系统。
