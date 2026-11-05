"""数据源实现包。

新增外部数据源时：在本包内新增一个实现 `DataSourceAdapter` 的文件，
并在 `app/main.py` 的 `build_registry()` 里注册即可，policy / loop / UI 都不用改。
"""
