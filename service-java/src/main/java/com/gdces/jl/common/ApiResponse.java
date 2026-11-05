package com.gdces.jl.common;

import java.util.List;
import java.util.Map;

/**
 * 统一响应信封：和 Python 侧保持一模一样，前端换实现不用改。
 *
 *   { "code": 0, "message": null, "data": {...} }
 */
public class ApiResponse {

    private int code;
    private String message;
    private Object data;

    public static ApiResponse ok(Object data) {
        ApiResponse r = new ApiResponse();
        r.code = 0;
        r.data = data;
        return r;
    }

    public static ApiResponse error(int code, String message) {
        ApiResponse r = new ApiResponse();
        r.code = code;
        r.message = message;
        return r;
    }

    public int getCode() { return code; }
    public String getMessage() { return message; }
    public Object getData() { return data; }

    public void setCode(int code) { this.code = code; }
    public void setMessage(String message) { this.message = message; }
    public void setData(Object data) { this.data = data; }

    /** 待补字段：值为空的那些 */
    public static List<String> missingOf(Map<String, String> fields) {
        if (fields == null) return List.of();
        return fields.entrySet().stream()
                .filter(e -> e.getValue() == null || e.getValue().trim().isEmpty())
                .map(Map.Entry::getKey)
                .toList();
    }
}
