package com.gdces.jl;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * 业务服务入口。
 *
 * 边界（见 contracts/openapi.yaml 的 x-owner=biz）：
 *   文书 / 项目 / 成员 / 审计 / 规范 / 统计
 *
 * 不负责：Agent 调度、工作区文件、git 提交。那些留在 Python 侧的 Agent 服务。
 */
@SpringBootApplication
public class JlBizApplication {
    public static void main(String[] args) {
        SpringApplication.run(JlBizApplication.class, args);
    }
}
