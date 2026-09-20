/**
 * 网易云 API 服务的启动器（发行版用）。
 *
 * ## 为什么不用 api-enhanced 自带的 app.js
 *
 * `app.js` 会 `serveNcmApi({checkVersion: true})`——启动时联网查新版本。
 * 发行版是要能离线用的，而且那次请求失败只会拖慢启动。这里照抄它的启动形状
 * （`generateConfig()` → `serveNcmApi()`），但把版本检查关掉。
 *
 * ## ★ 看门狗：为什么必须有
 *
 * node 是**子进程**。父进程（桌面应用）被强杀、崩溃、或者用户从任务管理器
 * 结束它时，node 不会跟着死——它会继续占着 4000 端口。下一次启动就会撞端口，
 * 而报错长得像"代码坏了"。
 *
 * 所以这里每 2 秒问一次父进程还在不在，不在就自己退出。
 * 这比"下次启动时清理上一个孤儿"更早生效：孤儿根本不会活到下一次启动。
 *
 * 用法：node launcher.js <父进程PID> [端口]
 */

const path = require('path');

const parentPid = Number(process.argv[2]) || 0;
const port = String(process.argv[3] || process.env.PORT || '4000');
process.env.PORT = port;

function log(msg) {
  // 交给宿主收着（Python 侧把 stdout 转进调试面板）
  process.stdout.write(`[网易云API] ${msg}\n`);
}

function watchParent() {
  if (!parentPid) {
    log('没拿到父进程 PID，跳过看门狗');
    return;
  }
  const timer = setInterval(() => {
    try {
      // signal 0 = 只探测存在性，不真的发信号
      process.kill(parentPid, 0);
    } catch {
      log(`父进程 ${parentPid} 已退出，本进程跟着退出（避免留下占着 ${port} 的孤儿）`);
      process.exit(0);
    }
  }, 2000);
  // 不要 unref：这个定时器就是"看门"本身，unref 掉等于没看
  timer.ref?.();
}

function main() {
  log(`启动中：端口 ${port}，node ${process.version}`);
  watchParent();

  const generateConfig = require(path.join(__dirname, 'api', 'generateConfig.js'));
  const server = require(path.join(__dirname, 'api', 'server.js'));

  Promise.resolve()
    .then(() => generateConfig())
    .then(() => {
      server.serveNcmApi({ checkVersion: false });
      log('已就绪');
    })
    .catch((err) => {
      log(`启动失败：${err && err.stack ? err.stack : err}`);
      // 退出码非 0，宿主那边能看出这不是正常结束
      process.exit(1);
    });
}

main();
