# E-CommerceBench selective upstream modules

Source repository: `QwenLM/E-CommerceBench`  
Reviewed commit: `b84678071c424fc94713b0a9e1ae86c519bd74c7`  
License: Apache-2.0; see [LICENSE](LICENSE).

The selected upstream demand and disruption implementation is preserved byte-for-byte in
`upstream/tools/ecommerce_env.py`. The adapter invokes `EcommerceEnv`, `StoreState`,
`_process_events_for_date`, `_process_sales_for_date`, `_get_event_demand_factor`, and
`_get_event_delivery_delay` for the supplier's private market simulation. The original
marketplace/order-processing/banking agent stack is not vendored or used.

Selected source file hashes (SHA-256):

- `counterpart.py`: `6eead3551b8c94c789d94b6635642ca0ab2d56f487c6119772d5c6e2d4ea93d4`
- `upstream/tools/ecommerce_env.py`: `0a1aea142c94ef746ff1ac0384d522e0454d02c3886bc814935d1ef230435552`
- `upstream/data/products.csv`: `a72a0104c5bcaf7828c2aa43c1261ea18c315119faf4fee6355a33836dd9300e`
- `upstream/data/category_params.csv`: `bfa822bc95969ca45b44f7d988037cd0126d1d2057d36449769dbdb292b361f6`
- `upstream/data/suppliers.csv`: `7323a98530104e7a8a0fba91957ddbc6501eaffa06e721ad502f355180f1db4e`
- `upstream/data/promotions.csv`: `93d8e3730a177e1b014ba1cbbb591ad21fd9c7a2f6e4db7c87eec2c033583647`
- `upstream/data/events.csv`: `455b24df72c72959ce829351a9510d8d97dec6ce163c8dd5c324477fc1bb3d58`
- `upstream/data/store_types.csv`: `680c7faf791b8f7d0ed244bcd3c45802697726bf992e4157a3d70ac9f71572d8`
- `upstream/data/store_type_config.py`: `a596c2151f1e8b3c60683f869d2dd560f9591ae0ba0156026d817007c2b281c5`

`upstream/tools/__init__.py` is an empty packaging marker rather than the upstream aggregate tool registry. This prevents importing the unused store/banking tool stack; the selected environment and negotiation implementations are unchanged.
