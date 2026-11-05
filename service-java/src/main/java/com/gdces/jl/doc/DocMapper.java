package com.gdces.jl.doc;

import org.apache.ibatis.annotations.*;

import java.util.List;
import java.util.Map;

/**
 * 文书数据访问。
 *
 * 用 MyBatis 注解式 mapper：这里有动态 SQL（只更新传了的字段），
 * 用 <script> 比 XML 省事，且和 Python 侧的语义一一对应。
 */
@Mapper
public interface DocMapper {

    @Select("""
            SELECT id, project_id AS projectId, kind, obj, state,
                   fields AS fieldsJson, bases AS basesJson, path, git_commit AS gitCommit
            FROM doc
            WHERE id = #{id}
            """)
    Doc findById(@Param("id") long id);

    @Select("""
            SELECT id, project_id AS projectId, kind, obj, state,
                   fields AS fieldsJson, bases AS basesJson, path, git_commit AS gitCommit
            FROM doc
            WHERE project_id = #{projectId}
            ORDER BY created_at DESC
            LIMIT #{limit}
            """)
    List<Doc> listByProject(@Param("projectId") String projectId, @Param("limit") int limit);

    @Insert("""
            INSERT INTO doc (project_id, kind, obj, state, fields, bases, path, git_commit)
            VALUES (#{projectId}, #{kind}, #{obj}, #{state}, #{fieldsJson}, #{basesJson},
                    #{path}, #{gitCommit})
            """)
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Doc doc);

    /** 只更新传进来的字段，不动其他列 */
    @Update("""
            <script>
            UPDATE doc
            <set>
              <if test="fieldsJson != null">fields = #{fieldsJson},</if>
              <if test="state != null">state = #{state},</if>
              <if test="path != null">path = #{path},</if>
              <if test="gitCommit != null">git_commit = #{gitCommit},</if>
              updated_at = NOW(3)
            </set>
            WHERE id = #{id}
            </script>
            """)
    int update(Doc doc);

    /** 统计：给 /stats 用 */
    @Select("""
            SELECT
              COUNT(*) AS docs,
              SUM(CASE WHEN state = '待补' THEN 1 ELSE 0 END) AS pending
            FROM doc WHERE project_id = #{projectId}
            """)
    Map<String, Object> countByProject(@Param("projectId") String projectId);
}
