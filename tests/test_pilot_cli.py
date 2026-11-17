from main import build_parser

def test_pilot_and_desktop_observation_commands_are_explicit_and_bounded():
    args=build_parser().parse_args(['pilot','--account','期刊','--inbox','capture.jsonl','--output-dir','out'])
    assert args.max_articles==3 and args.account=='期刊'
    observed=build_parser().parse_args(['desktop-observe','--hwnd','331714','--output','observation.json'])
    assert observed.hwnd==331714

def test_history_scope_comes_from_markdown_not_available_credentials(monkeypatch):
    from types import SimpleNamespace
    import main
    targets=[SimpleNamespace(biz='a'),SimpleNamespace(biz='b')]
    accounts=[SimpleNamespace(biz='b',canonical_name='B'),SimpleNamespace(biz='outside',canonical_name='Outside'),SimpleNamespace(biz='a',canonical_name='A')]
    class Repo:
        def sync_target_accounts(self,targets):pass
        def list_accounts(self,limit=None):return accounts
    class Progress:
        final=None
        def history_finished(self,completed,total):self.final=(completed,total)
        def __getattr__(self,name):return lambda *a,**k:None
    monkeypatch.setattr(main,'parse_profile_links_file',lambda path:targets)
    monkeypatch.setattr(main,'load_session_credentials',lambda path:[SimpleNamespace(fakeid='a')])
    visited=[]
    monkeypatch.setattr(main,'select_usable_session',lambda credentials,biz,**kwargs:visited.append(biz))
    args=SimpleNamespace(journal_links='targets.md',sessions_file='private-not-read',max_accounts=126,transport='requests',headed=False,resume=False)
    progress=Progress()
    assert main.cmd_crawl_history(args,Repo(),progress_logger=progress)==1
    assert visited==['a','b'] and progress.final==(0,2)
