import re
from functools import total_ordering


@total_ordering
class Version:
    def __init__(self,value):
        match=re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?',value)
        if not match:raise ValueError('版本号不符合 SemVer')
        self.value=value;self.core=tuple(int(match[i]) for i in (1,2,3));self.pre=match[4].split('.') if match[4] else []
        if any(not v or (v.isdigit() and len(v)>1 and v.startswith('0')) for v in self.pre):raise ValueError('预览版本号无效')
    def __eq__(self,other):return self.core==other.core and self.pre==other.pre
    def __lt__(self,other):
        if self.core!=other.core:return self.core<other.core
        if not self.pre or not other.pre:return bool(self.pre) and not other.pre
        for left,right in zip(self.pre,other.pre):
            if left==right:continue
            if left.isdigit() and right.isdigit():return int(left)<int(right)
            if left.isdigit()!=right.isdigit():return left.isdigit()
            return left<right
        return len(self.pre)<len(other.pre)


def compatible(target,current):
    for name,version in current.items():
        limits=target.get(name)
        if not isinstance(limits,dict) or type(limits.get('min')) is not int or type(limits.get('max')) is not int or not limits['min']<=version<=limits['max']:return False
    return True


def upgrade_order(releases,current):
    """Find an actual compatible intermediate order; never assume Hub is always first."""
    import itertools
    names=list(releases)
    for order in itertools.permutations(names):
        state={k:dict(v) for k,v in current.items()};valid=True
        for component in order:
            release=releases[component];state[component]=release['provides']
            for peer,capability in state.items():
                if peer==component:continue
                required=release.get('peers',{}).get(peer)
                if required and not compatible(required,capability):valid=False;break
            if not valid:break
        if valid:return {'order':list(order),'maintenance_all':False}
    return {'order':names,'maintenance_all':True,'reason':'不存在可保持在线写入的兼容中间状态'}
