-- 月度统计改为每实例每月一条完整记录（JSON），避免整月删改插。
CREATE TABLE cl1_data (
    instance TEXT NOT NULL REFERENCES storage_instances(instance),
    month TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY(instance, month)
) STRICT;
