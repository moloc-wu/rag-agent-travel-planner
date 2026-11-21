# cities_extra —— 先抓了餐饮、但还没有景点数据的城市

2026-11-11 补餐饮 POI 时（TODO 第 60 条），`fetch_foods.py` 按 `cities.known_names()`
把索引里的 144 个城市名全跑了一遍，结果抓回来 122 个城市只有餐饮、没有景点数据。

老大说「保存到另一个文件夹放着」，所以挪到这里，没删。

## 里面是什么

每个目录一个 `foods.json`，字段跟 `data/cities/<城市>/foods.json` 一样：

```json
{
  "_note": "...",
  "_datum": "gcj02",
  "city": "三亚",
  "pois": [
    {"id": "fd01", "name": "…", "category": "中餐厅", "tags": ["美食"],
     "lng": 109.5, "lat": 18.2, "avg_price": 78, "base": 74.0}
  ]
}
```

每个城市约 40 家，覆盖真实店名（高德 place/text 抓的，坐标 GCJ-02）。

## 怎么用

这些城市**进不了库**，因为 `import_data.py` 是以 `pois.json` 为入口的，
没有景点的城市进去也没意义（排不出行程）。

以后给某个城市补齐景点数据（`pois.json` / `hotels.json`）时：

1. 把 `cities_extra/<城市>/` 整个挪回 `data/cities/<城市>/`
2. 跑 `python import_data.py`

餐饮会自动并进 `poi` 表（`is_meal=1`），三餐推荐和「自己选店」就能用。

## 注意

- 别直接把这些目录留在 `data/cities/` 下：`import_data.py` 虽然会跳过没有
  `pois.json` 的目录，但 `data/cities` 是「有完整数据的城市」这个语义，混着会误导。
- `cities.known_names()` 的 144 个城市名来自城市索引，跟这里有没有数据无关。
