/**
 * fish-relay — 递话（id: 'fish-relay', order: 70）
 *
 * 触发：消息以触发词（默认「画一个」）开头 —— 插件在 intercept 阶段直接把这条消息吃掉，
 * 不唤醒模型（ev.stopped = true，零 token）。触发词后面的正文原样递给本地画图入口
 * `fishdraw`（配额/写词/出图/留档全在那条壳里），跑完把成品图发回聊天。
 *
 * 旗标（紧跟触发词、顺序不限、后面要带空格/冒号/逗号）：
 *   · `修改` —— 过写词关加工提示词（不带 = fishdraw --no-writer，原话直进）
 *   · `加工` —— 第一版之后再画第二遍（不带 = fishdraw --no-stage2，只出第一遍）
 *   「画一个 修改 加工 XXX」= 两个都开；「画一个 XXX」= 两个都不开。
 *   旗标后无分隔符不算旗标（「画一个 修改后的猫」整句当正文，不截字）。
 *
 * 黑匣纪律：
 *   ① 模型全程不介入：递出去的话不进 prompt、去向不回读、成品画面不打开不描述；
 *      模型只知道「刚才好像有人要了张图」这种留档痕迹。
 *   ② 排队在 fishdraw 里做（取号票 + 画图锁）：插件**不**自己排队，递到就 spawn，
 *      这样模型自然语言那条路和这条关键词路在同一个队伍里互相看得见；
 *      stdout 里的 `QUEUE <n>` 流式解析，n>0 就代发一句「前面排队n人」。
 *   ③ 出站只走 bot.deliver / bot.sendMedia；成品图先拷进 dataDir/fish-relay/ 再发
 *      （NapCat 容器挂载 dataDir → /app/plugin-workspace；/tmp/dsh-onebot-qq 那个
 *       挂载点是 docker 以 root 建的，宿主进程写不进去，不能用）。
 */

import fs from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { cqImage } from '../../platform/cq-file.js'

const DONE_RE = /(?:^|\n)DONE\s+(\S+)\s+(\d+)\s*(?:\n|$)/
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export default {
  id: 'fish-relay',
  title: '递话',
  order: 70,

  configSchema: {
    enabled: {
      type: 'bool', default: true, group: '工具',
      label: '递话触发词',
      hint: '开：消息以触发词开头时，这条直接递出去干活并把成品发回来，不叫醒模型。',
    },
    keyword: {
      type: 'str', default: '画一个', group: '工具',
      label: '触发词',
      hint: '只认消息开头这个词，后面跟要递的话（可含空格、标点、换行）。',
    },
    atRequiredInGroup: {
      type: 'bool', default: true, group: '工具',
      label: '群里必须@',
      hint: '开：群聊只有 @ 本鱼开头的递话才接；私聊始终直接接。',
    },
    allowedGroups: {
      type: 'str', default: '', group: '工具',
      label: '可接单的群（逗号分隔群号，空=全部）',
      hint: '只列出来的群可以递话；留空 = 所有群都接（仍受「群里必须@」约束）。',
    },
    masterOnly: {
      type: 'bool', default: false, group: '工具',
      label: '只给主人用',
      hint: '开：非主人递话会被挡回去。关：谁都能递，配额走 fishdraw 的账本。',
    },
    timeoutSec: {
      type: 'int', default: 3600, group: '工具',
      label: '超时（秒）',
      hint: '一单从递出去到出结果最多这么久（含排队等前单），超了杀掉并回报失败。',
    },
    ack: {
      type: 'str', default: '收到，画着呢……好了发回来', group: '工具',
      label: '接单回执',
      hint: '拦下消息后先回的这一句；空 = 不回执。',
    },
  },

  setup(bot) {
    const cfg = () => bot.cfg || {}

    /** 触发词前缀匹配：命中返回 { payload, flags }，没命中返回 null（整条放行给后面的管线）。
     *  旗标只认「修改」「加工」，各消耗一次、顺序不限；后面必须跟分隔符，否则当正文不截字。 */
    const FLAG_RE = /^(修改|加工)(?=[\s:：,，、]|$)/
    function matchPayload(text) {
      const kw = String(cfg().keyword ?? '画一个').trim()
      if (!kw) return null
      const m = new RegExp(`^${escRe(kw)}[\\s:：,，、]*([\\s\\S]+)$`).exec(String(text || '').trim())
      if (!m) return null
      let rest = m[1]
      const flags = { revise: false, refine: false }
      for (let i = 0; i < 2; i++) {
        const f = FLAG_RE.exec(rest)
        if (!f) break
        if (f[1] === '修改') flags.revise = true
        else flags.refine = true
        rest = rest.slice(f[0].length).replace(/^[\s:：,，、]+/, '')
      }
      const payload = String(rest || '').trim()
      return payload ? { payload, flags } : null
    }

    function groupAllowed(ev) {
      const list = String(cfg().allowedGroups || '').split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)
      if (list.length && !list.includes(String(ev.groupId))) return false
      if (cfg().atRequiredInGroup !== false && !ev.mentionedSelf) return false
      return true
    }

    /** 跑一单 fishdraw：resolve({ ok, img?, err?, quota? })，永不 reject。
     *  flags.revise = 过写词关（不带 → --no-writer）；flags.refine = 第二遍（不带 → --no-stage2）。
     *  排队是 fishdraw 自己的事（取号票 + 画图锁）：这里流式读 stdout，
     *  看到 `QUEUE <n>` 且 n>0 就替发一句「前面排队n人」（只发一次）。 */
    function runOnce(ev, text, userQq, flags) {
      return new Promise((resolve) => {
        const bin = path.join(bot.core.dataDir, 'bin', 'fishdraw')
        const timeoutMs = Math.max(30, Number(cfg().timeoutSec) || 3600) * 1000
        let out = '', err = '', killed = false, queueTold = false
        // detached + 杀进程组：超时要连 fishpipe/python 一起掐掉，不然孤儿继续烧卡
        const args = [bin, text]
        if (userQq) args.push('--user', String(userQq))
        if (!flags?.revise) args.push('--no-writer')
        if (!flags?.refine) args.push('--no-stage2')
        const child = spawn('bash', args, {
          env: process.env,
          stdio: ['ignore', 'pipe', 'pipe'],
          detached: true,
        })
        const timer = setTimeout(() => {
          killed = true
          try { process.kill(-child.pid, 'SIGKILL') } catch { /* 已经退了 */ }
        }, timeoutMs)
        child.stdout.on('data', (d) => {
          out = (out + d).slice(-20000)
          if (!queueTold) {
            const q = /QUEUE\s+(\d+)/.exec(out)
            if (q) {
              queueTold = true
              const n = Number(q[1])
              if (n > 0) bot.deliver(ev.chatKey, `前面排队${n}人，轮到就画`).catch(() => {})
            }
          }
        })
        child.stderr.on('data', (d) => { err = (err + d).slice(-8000) })
        child.on('error', (e) => {
          clearTimeout(timer)
          resolve({ ok: false, err: `进程起不来：${e.message}` })
        })
        child.on('close', (rc) => {
          clearTimeout(timer)
          if (killed) return resolve({ ok: false, err: '画超时了，这单被掐掉' })
          const done = DONE_RE.exec(out)
          if (rc === 0 && done) return resolve({ ok: true, img: done[1], bytes: Number(done[2]) })
          if (rc === 3) {
            const verdict = out.trim().split('\n').map((s) => s.trim()).filter(Boolean).pop() || '额度用完了'
            return resolve({ ok: false, quota: true, err: verdict })
          }
          const tail = (err || out).trim().split('\n').map((s) => s.trim()).filter(Boolean).pop() || `退出码 ${rc}`
          resolve({ ok: false, err: tail.slice(0, 120) })
        })
      })
    }

    /** 成品图 → dataDir（容器映射进 /app/plugin-workspace）→ sendMedia（不看画面、不回读内容）。 */
    async function ship(ev, img) {
      const dir = path.join(bot.core.dataDir, 'fish-relay')
      fs.mkdirSync(dir, { recursive: true })
      const staged = path.join(dir, `send-${Date.now()}-${path.basename(img)}`)
      fs.copyFileSync(img, staged)
      try {
        await bot.sendMedia(ev.chatKey, cqImage(staged), '')
      } finally {
        try { fs.unlinkSync(staged) } catch { /* 清不掉拉倒 */ }
      }
    }

    async function job(ev, text, flags) {
      const r = await runOnce(ev, text, ev.sender?.qq, flags)
      try {
        if (r.ok && r.img && fs.existsSync(r.img)) {
          await ship(ev, r.img)
        } else if (r.ok) {
          await bot.deliver(ev.chatKey, '画完了但图没找到，去喊主人看看')
        } else {
          await bot.deliver(ev.chatKey, (r.quota ? '' : '没画成：') + r.err)
        }
      } catch (e) {
        bot.log?.warn?.(`[递话] 发成品失败: ${e.message}`)
        try { await bot.deliver(ev.chatKey, '图画出来了但发不出去，去喊主人看看') } catch { /* 算了 */ }
      }
    }

    bot.on('intercept', async (ev) => {
      if (ev.stopped || cfg().enabled === false) return
      const hit = matchPayload(ev.text)
      if (!hit) return                           // 不是递话 → 放行，正常走唤醒/模型
      if (ev.isGroup && !groupAllowed(ev)) return // 群里没@或群不在白名单 → 也放行
      if (cfg().masterOnly === true && ev.sender?.isMaster !== true) {
        ev.stopped = true
        ev.stopReason = 'fish-relay:denied'
        try { await bot.deliver(ev.chatKey, '这条只给主人用～') } catch { /* 回执失败算了 */ }
        return
      }
      // 定下来要接这单：先掐断（不进模型），再回执，再排队干活
      ev.stopped = true
      ev.stopReason = 'fish-relay:claimed'
      const ack = String(cfg().ack ?? '').trim()
      if (ack) {
        try { await bot.deliver(ev.chatKey, ack) } catch (e) { bot.log?.warn?.(`[递话] 回执失败: ${e.message}`) }
      }
      job(ev, hit.payload, hit.flags).catch((e) => {
        bot.log?.warn?.(`[递话] 这单炸了: ${e.message}`)
      })
    })
  },
}
