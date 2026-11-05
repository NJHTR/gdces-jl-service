package com.gdces.jl.doc;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 文书业务规则。这一层是「真值」：
 *   - 状态由字段推导：有空字段 → 待补；全填完 → 待签
 *   - 只能改本文书已有的字段（防止乱塞列）
 *
 * 文件与 git 提交不在这里 —— 那是 Agent 侧的事，这里只管数据库。
 */
@Service
public class DocService {

    private final DocMapper mapper;
    private final ObjectMapper json = new ObjectMapper();

    public DocService(DocMapper mapper) {
        this.mapper = mapper;
    }

    /** 登记：Agent 侧把文件写好、git 提交完，再来这里落一行 */
    @Transactional
    public Map<String, Object> register(Map<String, Object> body) {
        String projectId = str(body.get("projectId"));
        String kind = str(body.get("kind"));
        if (projectId == null || projectId.isBlank()) throw new IllegalArgumentException("projectId 必填");
        if (kind == null || kind.isBlank()) throw new IllegalArgumentException("kind 必填");

        @SuppressWarnings("unchecked")
        Map<String, String> fields = (Map<String, String>) body.getOrDefault("fields", Map.of());
        Object bases = body.getOrDefault("bases", List.of());

        List<String> missing = missingOf(fields);
        String state = str(body.get("state"));
        if (state == null || state.isBlank()) state = missing.isEmpty() ? "待签" : "待补";

        Doc doc = new Doc();
        doc.setProjectId(projectId);
        doc.setKind(kind);
        doc.setObj(str(body.get("obj")));
        doc.setState(state);
        doc.setFieldsJson(writeJson(fields));
        doc.setBasesJson(writeJson(bases));
        doc.setPath(str(body.get("path")));
        doc.setGitCommit(str(body.get("commit")));

        mapper.insert(doc);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("docId", doc.getId());
        out.put("state", state);
        out.put("missing", missing);
        return out;
    }

    /** 详情：把 JSON 列还原成对象，附上待补清单 */
    public Map<String, Object> detail(long id) {
        Doc doc = mapper.findById(id);
        if (doc == null) return null;

        Map<String, String> fields = readMap(doc.getFieldsJson());
        List<Object> bases = readList(doc.getBasesJson());

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", doc.getId());
        out.put("projectId", doc.getProjectId());
        out.put("kind", doc.getKind());
        out.put("obj", doc.getObj());
        out.put("state", doc.getState());
        out.put("fields", fields);
        out.put("missing", missingOf(fields));
        out.put("bases", bases);
        out.put("path", doc.getPath());
        out.put("commit", shortCommit(doc.getGitCommit()));
        return out;
    }

    /** 保存：只改已有字段，改完重算状态 */
    @Transactional
    public Map<String, Object> patch(long id, Map<String, Object> body) {
        Doc doc = mapper.findById(id);
        if (doc == null) return null;

        @SuppressWarnings("unchecked")
        Map<String, String> patch = (Map<String, String>) body.get("fields");
        if (patch == null || patch.isEmpty()) throw new IllegalArgumentException("fields 必填");

        Map<String, String> fields = new LinkedHashMap<>(readMap(doc.getFieldsJson()));
        for (Map.Entry<String, String> e : patch.entrySet()) {
            if (fields.containsKey(e.getKey())) {
                fields.put(e.getKey(), e.getValue() == null ? "" : e.getValue());
            }
        }

        List<String> missing = missingOf(fields);
        String state = str(body.get("state"));
        if (state == null || state.isBlank()) state = missing.isEmpty() ? "待签" : "待补";

        Doc update = new Doc();
        update.setId(id);
        update.setFieldsJson(writeJson(fields));
        update.setState(state);
        update.setPath(str(body.get("path")));
        update.setGitCommit(str(body.get("commit")));
        mapper.update(update);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("id", id);
        out.put("state", state);
        out.put("missing", missing);
        out.put("fields", fields);
        return out;
    }

    public List<Doc> list(String projectId, int limit) {
        return mapper.listByProject(projectId, Math.min(Math.max(limit, 1), 200));
    }

    public Map<String, Object> stats(String projectId) {
        return mapper.countByProject(projectId);
    }

    // ---------------------------------------------------------------- 工具
    private List<String> missingOf(Map<String, String> fields) {
        if (fields == null) return List.of();
        return fields.entrySet().stream()
                .filter(e -> e.getValue() == null || e.getValue().trim().isEmpty())
                .map(Map.Entry::getKey)
                .toList();
    }

    private String writeJson(Object o) {
        try {
            return json.writeValueAsString(o);
        } catch (Exception e) {
            return "{}";
        }
    }

    private Map<String, String> readMap(String s) {
        if (s == null || s.isBlank()) return new LinkedHashMap<>();
        try {
            return json.readValue(s, new TypeReference<LinkedHashMap<String, String>>() {});
        } catch (Exception e) {
            return new LinkedHashMap<>();
        }
    }

    private List<Object> readList(String s) {
        if (s == null || s.isBlank()) return List.of();
        try {
            return json.readValue(s, new TypeReference<List<Object>>() {});
        } catch (Exception e) {
            return List.of();
        }
    }

    private String shortCommit(String c) {
        if (c == null || c.isBlank()) return "";
        return c.length() > 10 ? c.substring(0, 10) : c;
    }

    private String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }
}
