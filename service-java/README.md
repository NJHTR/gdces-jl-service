# jl-biz · 业务服务（Java）

监理 AI 服务的**业务域**：文书 / 项目 / 成员 / 审计 / 规范 / 统计。

## 为什么单独一个服务

分包边界见 `contracts/openapi.yaml` 的 `x-owner`：

| 归属 | 内容 | 语言 |
|---|---|---|
`biz` | 文书状态机、字段校验、权限、审计 | **Java（这里）** |
`agent` | 任务编排、引擎调用、工具轨迹、工作区 git | Python |
`infra` | 鉴权、上传、blob、地理 | Python |

业务域抽出来的理由：事务、类型安全、MyBatis 写复杂查询/报表更顺，而且
后续要接电子签章、PDF 导出、国密算法，生态都在 Java。

## 当前实现范围

只做了文书（doc）一个域，用来验证"这条路走得通"：

```
GET   /api/v1/docs?projectId=&limit=   列表
GET   /api/v1/docs/{id}                详情（含待补清单）
POST  /api/v1/docs/register            登记（Agent 侧写完文件后调这个）
PATCH /api/v1/docs/{id}                保存字段，自动重算状态
```

响应结构和 Python 侧**完全一致**：`{code, message, data}`，前端不用改。

## 关键约定

**① 这个服务不碰文件**

工作区里的 `.md` 和 git 提交由 Python 侧的 Agent 服务负责。
Java 只管数据库那一行 —— 所以 `doc` 表只有一个写者。

**② 状态由字段推导**

```java
if (missing.isEmpty()) state = "待签";
else                   state = "待补";
```

不允许调用方随便指定状态（除了显式传 `state` 用于作废等流程）。

**③ 只改已有字段**

`PATCH` 时，不在原 `fields` 里的键会被忽略 —— 防止乱塞列。

## 构建与运行

服务器上 JDK 和 Maven 装在 `~/opt`（没有 root，全部用户态安装）：

```bash
export JAVA_HOME=/home/baize/opt/jdk-17.0.2
export PATH=$JAVA_HOME/bin:/home/baize/opt/apache-maven-3.9.6/bin:$PATH
export MYSQL_PASSWORD=$(grep '^MYSQL_PASSWORD=' ../.env | cut -d= -f2)

cd service-java
mvn -s settings.xml -DskipTests package
setsid nohup java -jar target/jl-biz-0.1.0.jar > /tmp/jl-biz.log 2>&1 < /dev/null &
```

- 端口 `8082`（Python 的 BFF 在 8081）
- 数据库和 Python 侧共用：`127.0.0.1:3309/jl`

## 踩过的坑

**① JDBC 的 `characterEncoding` 不是 `utf8mb4`**

```
Unsupported character encoding 'utf8mb4'
```

JDBC 要的是 **Java 字符集名**：

```
characterEncoding=UTF-8&connectionCollation=utf8mb4_general_ci
```

**② 后台启动要 `setsid ... < /dev/null`**

只 `nohup &` 时进程会跟着 SSH 会话退出。

**③ 端口没释放就重启会失败**

`pkill` 之后要等几秒，或者先确认端口空了再启。

## 下一步

1. 把 `project / member / audit / standard / stats` 也搬过来
2. Python 侧的 `biz` 调用改成 HTTP（加一个 `BIZ_BASE_URL` 开关，可灰度切）
3. 事务边界：文书状态流转 + 审计写一条，应该在一个事务里
