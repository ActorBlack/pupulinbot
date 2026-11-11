"""Import the actual NoneBot plugin in an isolated process, without opening a server."""

import os
import subprocess
import sys


def test_plugin_loads_with_real_dependencies(tmp_path):
    script = """
import nonebot
from nonebot.adapters.onebot.v11 import Adapter
nonebot.init(driver='~fastapi')
nonebot.get_driver().register_adapter(Adapter)
assert nonebot.load_plugin('pupulinbot.plugin') is not None
from nonebot.rule import TrieRule
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from types import SimpleNamespace
for command in ('help', 'mshelp', 'msbind', 'msunbind', 'ms2', 'mslast', 'msstats', 'ms4',
                'msstyle', 'msrank', 'msstatus', 'mswatch',
                'mslife', 'msreview', 'mstable', 'thwatch', 'thstatus', 'thlast',
                'thbind', 'thunbind', 'th2', 'th', 'th4', 'th3', 'thlife', 'thdetail', 'thrank',
                'thlive'):
    event = SimpleNamespace(get_type=lambda: 'message',
                            get_message=lambda: Message('/' + command))
    assert TrieRule.get_value(None, event, {})['command'] is not None, command
event = SimpleNamespace(get_type=lambda: 'message', get_message=lambda: Message('/msmonitor on'))
assert TrieRule.get_value(None, event, {})['command'] != ('msmonitor',)
# Longer commands must retain their own argument, rather than be captured by ms.
event = SimpleNamespace(get_type=lambda: 'message',
                        get_message=lambda: Message('/mslife 普普林 last=100'))
parsed = TrieRule.get_value(None, event, {})
assert parsed['command'] == ('mslife',)
assert str(parsed['command_arg']) == '普普林 last=100'
event = SimpleNamespace(
    get_type=lambda: 'message',
    get_message=lambda: Message('ms2 ') + MessageSegment.at(234567) + ' 。不记仇的钟会',
)
parsed = TrieRule.get_value(None, event, {})
assert parsed['command'] == ('ms2',)
assert parsed['command_arg'][0].data['qq'] == '234567'
assert parsed['command_arg'].extract_plain_text().strip() == '。不记仇的钟会'
import asyncio
from nonebot.message import _apply_run_preprocessors
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from pupulinbot.plugin import (
    settings, help_cmd, help_handler, notify_batch, send_auto_review, superusers, batches, db,
    manage_vip2, vip2_cmd,
)
assert "285843647" in superusers and "285843647" in batches.superusers
settings.monitor_only_groups = {'706530575'}
async def verify_group_policy():
    from unittest.mock import AsyncMock, patch
    from nonebot.adapters.onebot.v11 import MessageSegment
    from nonebot.exception import FinishedException
    await db.initialize()
    admin = GroupMessageEvent.model_construct(group_id=706530575, user_id=285843647)
    ordinary = GroupMessageEvent.model_construct(group_id=706530575, user_id=123456)
    async def call_vip2(event, args):
        finish = AsyncMock(side_effect=FinishedException)
        with patch.object(vip2_cmd, 'finish', new=finish):
            try:
                await manage_vip2(event, args)
            except FinishedException:
                return finish.call_args.args[0]
        raise AssertionError('Missing command completion')
    assert '超级管理员' in await call_vip2(ordinary, Message('add 234567'))
    assert not await db.is_vip2('234567')
    assert '已加入' in await call_vip2(admin, Message('add ') + MessageSegment.at(234567))
    assert '已在名单' in await call_vip2(admin, Message('add 234567'))
    assert '234567' in await call_vip2(admin, Message('list'))
    assert await db.is_vip2('234567')
    with patch('pupulinbot.plugin.send_help', new=AsyncMock()) as send_help, \
         patch.object(help_cmd, 'finish', new=AsyncMock()):
        for group, caller in ((706530575, 123456), (706530575, 234567), (546573626, 285843647)):
            event = GroupMessageEvent.model_construct(group_id=group, user_id=caller)
            await help_handler(None, event)
        options = [call.kwargs for call in send_help.await_args_list]
        assert options[0] == options[1] == options[2]
        from pupulinbot.help_menu import help_sections
        text = '\\n'.join(body for _, body in help_sections(**options[0]))
        assert all(cmd in text for cmd in ('/ms2', '/mortal', '/mswatch', '/vip2'))
    for group in (706530575, 546573626):
        for caller in (285843647, 123456, 234567):
            commands = ('help', 'mortal', 'msreview', 'msmortal', 'mstable', 'mortalstatus', 'vip2',
                        'msbind', 'ms2', 'mslife', 'thbind', 'th4', 'th3', 'thlife', 'thrank',
                        'mswatch', 'thwatch', 'msstatus', 'thstatus', 'thlive')
            for command in commands:
                event = GroupMessageEvent.model_construct(
                    post_type='message', group_id=group, user_id=caller,
                    message=Message('/' + command),
                )
                state = {}
                TrieRule.get_value(None, event, state)
                allowed = await _apply_run_preprocessors(
                    bot=None, event=event, state=state, matcher=help_cmd(),
                )
                monitoring = command in ('mswatch', 'thwatch', 'msstatus', 'thstatus', 'thlive')
                mortal = command in ('mortal', 'msreview', 'msmortal', 'mstable', 'mortalstatus')
                expected = caller == 285843647 or (
                    command != 'vip2'
                    and (not monitoring or (group != 706530575 and caller != 234567))
                    and (not mortal or group != 706530575 or caller == 234567)
                )
                assert allowed == expected, (group, caller, command)
    # A monitoring-only group never receives unrelated background Mortal messages.
    await notify_batch({'group_id': '706530575'})
    await send_auto_review(None, 706530575, 'name', None, 42)
    from pupulinbot.plugin import build_monitor_notification
    class FakeBot:
        send_group_msg = AsyncMock()
    fake = FakeBot()
    with patch('pupulinbot.plugin.Bot', FakeBot), \
         patch('pupulinbot.plugin.get_driver', return_value=SimpleNamespace(bots={'1':fake})):
        job = {'id':'abc', 'group_id':'706530575', 'owner':'285843647',
               'status':'paused', 'rows':[]}
        await notify_batch(job)
        fake.send_group_msg.assert_awaited_once()
        await notify_batch({**job, 'owner':'123456'})
        fake.send_group_msg.assert_awaited_once()
        await notify_batch({**job, 'owner':'234567'})
        assert fake.send_group_msg.await_count == 2
        assert '已从' in await call_vip2(admin, Message('del 234567'))
        assert not await db.is_vip2('234567')
        await notify_batch({**job, 'owner':'234567'})
        assert fake.send_group_msg.await_count == 2
        for group in ('706530575', '546573626'):
            fake.send_group_msg.reset_mock()
            done = {**job, 'group_id':group, 'status':'done',
                    'rows':[{'status':'done'}] * 4}
            with patch('pupulinbot.plugin.render_batch_chart', return_value=b'png'):
                await notify_batch(done)
            fake.send_group_msg.assert_awaited_once()
            sent = fake.send_group_msg.call_args.kwargs
            assert sent['group_id'] == int(group)
            assert [segment.type for segment in sent['message']] == ['at','text','image']
            assert sent['message'][0].data['qq'] == '285843647'
        fake.send_group_msg.reset_mock()
        with patch('pupulinbot.plugin.render_batch_chart') as render:
            await notify_batch({**job, 'status':'retry_wait', 'message':'30秒后重试'})
            fake.send_group_msg.assert_not_awaited()
            render.assert_not_called()
        with patch('pupulinbot.plugin.scheduler.add_job') as schedule:
            await notify_batch({**job, 'status':'retry_wait', 'retry_phase':'verification',
                                'next_retry_at':'2026-11-12T00:00:00+00:00'})
            fake.send_group_msg.assert_not_awaited()
            assert schedule.call_args.args[1] == 'date'
            assert schedule.call_args.kwargs['run_date'].isoformat() == '2026-11-12T00:00:00+00:00'
        await notify_batch({**job, 'status':'verification_paused', 'owner':'123456',
                            'message':'mortal resume abc'})
        fake.send_group_msg.assert_awaited_once()
        assert fake.send_group_msg.call_args.kwargs['group_id'] == 1131357545
        assert 'mortal resume abc' in fake.send_group_msg.call_args.kwargs['message']
        fake.send_group_msg.reset_mock()
        with patch('pupulinbot.plugin.render_batch_chart', side_effect=RuntimeError('render')):
            await notify_batch(done)
        fake.send_group_msg.assert_awaited_once()
        fallback = fake.send_group_msg.call_args.kwargs['message']
        assert fallback[0].type == 'at' and fallback[0].data['qq'] == '285843647'
        assert '分析结果已保存' in fallback.extract_plain_text()
        assert 'mortal report abc' in fallback.extract_plain_text()
    with patch('pupulinbot.plugin.paipuya.promotion', new=AsyncMock(return_value='雀圣3星')), \
         patch('pupulinbot.plugin.collect_profiles', new=AsyncMock(return_value={})), \
         patch('pupulinbot.plugin.render_monitor_card', return_value=b'png'):
        message = await build_monitor_notification(
            {'account_id':42}, object(), 'finish', '结算结果'
        )
        assert str(message).endswith('结算结果\\n欢迎新雀圣3星')
asyncio.run(verify_group_policy())

from nonebot.matcher import matchers
async def verify_command_boundaries():
    for text in ('/msrecords', '/msrecords 喵杰', 'msrecords',
                 '/mssummary', '/mssummary 喵杰', 'mssummary', '/thunknown', 'thunknown'):
        event = GroupMessageEvent.model_construct(
            post_type='message', group_id=546573626, user_id=285843647,
            message=Message(text),
        )
        state = {}
        TrieRule.get_value(None, event, state)
        for bucket in matchers.values():
            for matcher in bucket:
                assert not await matcher.rule(None, event, state.copy()), text
    for text in ('/mslast 喵杰', '/mslast', '/ms 喵杰', '/ms',
                 '/ms4', '/ms4 喵杰 -i', 'ms4 喵杰', '/th', '/th ププリン -l 100',
                 '/th4 ププリン -i', '/th3', '/thbind ププリン', '/thlast ププリン -l 5'):
        event = GroupMessageEvent.model_construct(
            post_type='message', group_id=546573626, user_id=285843647,
            message=Message(text),
        )
        state = {}
        TrieRule.get_value(None, event, state)
        results = [await matcher.rule(None, event, state.copy())
                   for bucket in matchers.values() for matcher in bucket]
        assert any(results), text
asyncio.run(verify_command_boundaries())

async def verify_mortal_short_options():
    from unittest.mock import AsyncMock, patch
    from pupulinbot.plugin import ms_mortal, msmortal_cmd, paipuya, MahjongQueries
    from pupulinbot.models import Player
    event = GroupMessageEvent.model_construct(
        post_type='message', group_id=1131357545, user_id=285843647,
        message=Message('/msmortal 玩家 -l 3 -r 玉'),
    )
    create_job = AsyncMock(return_value={'id':'test','rows':[]})
    with patch.object(MahjongQueries, 'player', new=AsyncMock(return_value=Player(42,'玩家'))), \
         patch.object(paipuya, 'history', new=AsyncMock(return_value=([],False))) as history, \
         patch.object(batches, 'create', new=create_job) as create, \
         patch.object(batches, 'start'), \
         patch.object(msmortal_cmd, 'finish', new=AsyncMock()) as finish:
        await ms_mortal(event, Message('玩家 -l 3 -r 玉'))
        history.assert_awaited_once_with(42, 3, start=None, end=None, modes=[12])
        assert create.call_args.args[0] == 3
        for text, count in [('玩家', 10), ('玩家 -l 100', 100)]:
            history.reset_mock()
            create.reset_mock()
            await ms_mortal(event, Message(text))
            assert history.call_args.args[1] == count
            assert create.call_args.args[0] == count
        for text in ('玩家 -l 101', '玩家 last=0', '玩家 -l -1'):
            history.reset_mock()
            create.reset_mock()
            await ms_mortal(event, Message(text))
            history.assert_not_awaited()
            create.assert_not_awaited()
            assert '100' in finish.call_args.args[0]
asyncio.run(verify_mortal_short_options())

async def verify_mortal_batch_default_and_limit():
    from unittest.mock import AsyncMock, patch
    from nonebot.exception import FinishedException
    from pupulinbot.plugin import batch_command, db, review_cmd
    event = GroupMessageEvent.model_construct(
        post_type='message', group_id=1131357545, user_id=285843647,
        message=Message('/mortal batch'),
    )
    binding = AsyncMock(return_value={'account_id':42,'nickname':'玩家'})
    create = AsyncMock(return_value={'id':'test','rows':[]})
    with patch.object(db, 'binding', new=binding), \
         patch.object(batches, 'create', new=create), \
         patch.object(batches, 'start'), \
         patch.object(review_cmd, 'send', new=AsyncMock()), \
         patch.object(review_cmd, 'finish', new=AsyncMock(side_effect=FinishedException)):
        for parts, count in [(['batch'],10), (['batch','100'],100)]:
            try:
                await batch_command(event, parts)
            except FinishedException:
                pass
            else:
                raise AssertionError('Missing command completion')
            assert create.call_args.args[0] == count
        binding.reset_mock()
        create.reset_mock()
        for value in ('101','0','-1'):
            try:
                await batch_command(event, ['batch',value])
            except ValueError:
                pass
            else:
                raise AssertionError('Over-limit request was accepted')
        binding.assert_not_awaited()
        create.assert_not_awaited()
asyncio.run(verify_mortal_batch_default_and_limit())

async def verify_tenhou_picture_delivery():
    import json
    from unittest.mock import AsyncMock, patch
    from nonebot.utils import DataclassEncoder
    from pupulinbot.plugin import poll_tenhou, tenhou_monitor
    from pupulinbot.tenhou_monitor import TenhouStartNotice
    class FakeBot:
        send_group_msg = AsyncMock(return_value={'message_id': 123})
        get_msg = AsyncMock(return_value={
            'group_id': 1131357545,
            'message': [{'type':'image','data':{'file':'test.png'}}],
        })
    fake = FakeBot()
    caption = '四特南喰赤 · 对局开始\\nlingran / [CQ:at,qq=all]'
    async def poll(send):
        await send('1131357545', TenhouStartNotice(b'png', caption))
    with patch('pupulinbot.plugin.Bot', FakeBot), \
         patch('pupulinbot.plugin.get_driver', return_value=SimpleNamespace(bots={'1':fake})), \
         patch.object(tenhou_monitor, 'poll', new=poll):
        await poll_tenhou()
    payload = json.loads(json.dumps(fake.send_group_msg.call_args.kwargs,
                                    cls=DataclassEncoder))
    assert isinstance(payload['message'], list)
    assert [s['type'] for s in payload['message']] == ['text', 'image']
    assert payload['message'][0]['data']['text'] == caption + '\\n'
    fake.get_msg.assert_awaited_once_with(message_id=123, _timeout=4)
asyncio.run(verify_tenhou_picture_delivery())

from pupulinbot.help_menu import BATCH_ACTIONS
assert 'report' in BATCH_ACTIONS and '报告' not in BATCH_ACTIONS
for command in ('雀魂绑定', '绑定雀魂', '雀魂解绑', '解绑雀魂', '雀魂牌谱', '牌谱屋', '最近牌谱',
                '雀魂监控', '开局提醒', '牌谱分析', '麻将帮助', '雀魂帮助', '雀魂统计', '雀魂信息',
                '群雀魂排行', '雀魂排行', '雀魂关注监控', '雀魂状态', '跑谱', '全桌跑谱',
                '雀魂近况', '雀魂摘要', '雀魂同桌', '雀魂对比'):
    for prefix in ('', '/'):
        event = SimpleNamespace(get_type=lambda: 'message',
                                get_message=lambda: Message(prefix + command))
        assert TrieRule.get_value(None, event, {})['command'] is None, command


"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PUPULIN_DATABASE": str(tmp_path / "startup.db"),
            "SUPERUSERS": '["285843647"]',
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
