import os
import uvicorn
# 云平台会自动分配 PORT 环境变量，本地默认回退到 9000
port = int(os.environ.get("PORT", 9000))
uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
