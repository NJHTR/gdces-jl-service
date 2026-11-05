package com.gdces.jl.doc;

/**
 * 文书。字段说明见 contracts/openapi.yaml 的 Doc schema。
 *
 * fields / bases 在库里是 JSON 列，这里按字符串存，
 * 由服务层用 Jackson 转成对象返回（避免引入 MyBatis 的 JSON 类型处理器）。
 */
public class Doc {

    private Long id;
    private String projectId;
    private String kind;
    private String obj;
    private String state;          // 待补 / 待签 / 已签 / 作废
    private String fieldsJson;
    private String basesJson;
    private String path;           // 工作区相对路径（由 Agent 侧写入）
    private String gitCommit;

    public Long getId() { return id; }
    public void setId(Long id) { this.id = id; }

    public String getProjectId() { return projectId; }
    public void setProjectId(String projectId) { this.projectId = projectId; }

    public String getKind() { return kind; }
    public void setKind(String kind) { this.kind = kind; }

    public String getObj() { return obj; }
    public void setObj(String obj) { this.obj = obj; }

    public String getState() { return state; }
    public void setState(String state) { this.state = state; }

    public String getFieldsJson() { return fieldsJson; }
    public void setFieldsJson(String fieldsJson) { this.fieldsJson = fieldsJson; }

    public String getBasesJson() { return basesJson; }
    public void setBasesJson(String basesJson) { this.basesJson = basesJson; }

    public String getPath() { return path; }
    public void setPath(String path) { this.path = path; }

    public String getGitCommit() { return gitCommit; }
    public void setGitCommit(String gitCommit) { this.gitCommit = gitCommit; }
}
