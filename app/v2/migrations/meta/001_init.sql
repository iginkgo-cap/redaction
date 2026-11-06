-- 控制库（ADR-007 §1）：只记删除墓碑，不含客户数据
CREATE TABLE namespace (
    id         text PRIMARY KEY,
    slug       text NOT NULL,
    removed_at timestamptz NOT NULL
);
