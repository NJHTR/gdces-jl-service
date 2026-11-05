package com.gdces.jl.doc;

import com.gdces.jl.common.ApiResponse;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 文书接口。路径和响应结构与 Python 侧完全一致，前端不用改。
 *
 *   GET   /api/v1/docs?projectId=&limit=
 *   GET   /api/v1/docs/{id}
 *   POST  /api/v1/docs/register
 *   PATCH /api/v1/docs/{id}
 */
@RestController
@RequestMapping("/api/v1/docs")
public class DocController {

    private final DocService service;

    public DocController(DocService service) {
        this.service = service;
    }

    @GetMapping
    public ApiResponse list(@RequestParam String projectId,
                            @RequestParam(defaultValue = "50") int limit) {
        List<Doc> docs = service.list(projectId, limit);
        List<Map<String, Object>> brief = docs.stream().map(d -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", d.getId());
            m.put("kind", d.getKind());
            m.put("obj", d.getObj());
            m.put("state", d.getState());
            return m;
        }).toList();

        Map<String, Object> data = new LinkedHashMap<>();
        data.put("list", brief);
        data.put("total", brief.size());
        return ApiResponse.ok(data);
    }

    @GetMapping("/{id:\\d+}")
    public ResponseEntity<ApiResponse> detail(@PathVariable long id) {
        Map<String, Object> doc = service.detail(id);
        if (doc == null) {
            return ResponseEntity.status(HttpStatus.NOT_FOUND)
                    .body(ApiResponse.error(404, "文书不存在"));
        }
        return ResponseEntity.ok(ApiResponse.ok(doc));
    }

    @PostMapping("/register")
    public ResponseEntity<ApiResponse> register(@RequestBody Map<String, Object> body) {
        try {
            return ResponseEntity.ok(ApiResponse.ok(service.register(body)));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(ApiResponse.error(400, e.getMessage()));
        }
    }

    @PatchMapping("/{id:\\d+}")
    public ResponseEntity<ApiResponse> patch(@PathVariable long id,
                                             @RequestBody Map<String, Object> body) {
        try {
            Map<String, Object> out = service.patch(id, body);
            if (out == null) {
                return ResponseEntity.status(HttpStatus.NOT_FOUND)
                        .body(ApiResponse.error(404, "文书不存在"));
            }
            return ResponseEntity.ok(ApiResponse.ok(out));
        } catch (IllegalArgumentException e) {
            return ResponseEntity.badRequest().body(ApiResponse.error(400, e.getMessage()));
        }
    }
}
