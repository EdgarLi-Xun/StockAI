"""Authenticated backend handover preserves the returning device's IDs and offline DAG."""
import base64
import json
from .protocol import SyncError,digest,seal,open_sealed,keyring,key_text,validate_operation


class Lineage:
    def add_lineages(self,old,head,new,mapping,source,target,secret):
        sources=[{'space':old['space_id'],'mapping':mapping,'secret':self.secrets(old)}]
        for item in head.get('lineages',[]):
            raw,_=source.read('objects/'+item['map']+'.bin')
            if digest(raw)!=item['map']:raise SyncError('CORRUPT')
            prior=open_sealed(keyring(self.secrets(old)),old['space_id'],raw,'handover-map')
            previous=self.read_private(old['user_id'],'lineagekeys-'+item['space'])
            if previous is None:
                row=self.read_private(old['user_id'],'binding-'+item['space'])
                if row:previous=self.secrets(row)
            if previous is None:raise SyncError('KEY','缺少前次迁移恢复材料，不能丢弃原空间的标识关系')
            sources.append({'space':item['space'],'mapping':{h:mapping[v] for h,v in prior['mapping'].items() if v in mapping},'secret':previous})
        new['lineages']=[]
        for entry in sources:
            body=seal(keyring(secret),new['space'],{'source':entry['space'],'target':new['space'],'mapping':entry['mapping']},'handover-map');content_hash=digest(body);target.put('objects/'+content_hash+'.bin',body,create=True)
            previous=entry['secret'];generation=previous.get('generation',1)
            proof=seal(keyring(previous),entry['space'],{'target':new['space'],'target_key':digest(keyring(secret)[1]),'map':content_hash},'handover-proof',generation)
            new['lineages'].append({'space':entry['space'],'map':content_hash,'proof':base64.b64encode(proof).decode()})
            self.write_private(old['user_id'],'lineagekeys-'+entry['space'],{'key':previous['key'],'keys':{str(k):key_text(v) for k,v in keyring(previous).items()},'generation':generation})

    def lineage_material(self,uid,head):
        result={}
        for item in head.get('lineages',[]):
            value=self.read_private(uid,'lineagekeys-'+item['space'])
            if value is not None:result[item['space']]=value
        return result

    def receive_lineage(self,row,remote,head,material):
        uid=row['user_id'];current_keys=keyring(self.secrets(row))
        for item in head.get('lineages',[]):
            old_space=item['space'];previous=material.get('lineage_keys',{}).get(old_space)
            if previous is None:raise SyncError('KEY','新空间的恢复材料缺少前次迁移密钥，请从迁移端重新导出')
            proof=open_sealed(keyring(previous),old_space,base64.b64decode(item['proof']),'handover-proof')
            if proof!={'target':row['space_id'],'target_key':digest(current_keys[1]),'map':item['map']}:raise SyncError('CORRUPT')
            self.write_private(uid,'lineagekeys-'+old_space,previous)
            old_records=self.journal.all('sync',uid,'operation:'+old_space+':')
            if not old_records:continue  # 新设备没有旧的本地标识，不据邮箱或同名内容匹配。
            raw,_=remote.read('objects/'+item['map']+'.bin')
            if digest(raw)!=item['map']:raise SyncError('CORRUPT')
            archive=open_sealed(current_keys,row['space_id'],raw,'handover-map');mapping=dict(archive['mapping']);converted={};old_keys=keyring(previous)
            if archive['source']!=old_space or archive['target']!=row['space_id']:raise SyncError('CORRUPT')
            by_hash={slot.rsplit(':',1)[1]:value for slot,value in old_records.items()}
            def convert(op_hash):
                if op_hash in converted:return converted[op_hash]
                record=by_hash.get(op_hash)
                if not record:raise SyncError('DEPENDENCY','旧设备缺少本地依赖，请先补齐其恢复材料')
                original=validate_operation(open_sealed(old_keys,old_space,base64.b64decode(record['cipher'])),old_space)
                if op_hash in mapping:
                    ident=mapping[op_hash];cipher,_=remote.read('objects/'+ident+'.bin')
                    if digest(cipher)!=ident:raise SyncError('CORRUPT')
                    value=validate_operation(open_sealed(current_keys,row['space_id'],cipher),row['space_id']);state='received'
                else:
                    value={**original,'space':row['space_id'],'parents':[convert(h) for h in original['parents']],'restores':convert(original['restores']) if original['restores'] else None}
                    cipher=seal(current_keys,row['space_id'],value,generation=self.secrets(row).get('generation',1));ident=digest(cipher);state='ready'
                saved={'cipher':base64.b64encode(cipher).decode(),'state':state}
                if record.get('local_map'):
                    local=open_sealed(old_keys,old_space,base64.b64decode(record['local_map']),'local-map')
                    saved['local_map']=base64.b64encode(seal(current_keys,row['space_id'],local,'local-map',self.secrets(row).get('generation',1))).decode()
                self.journal.put('sync',uid,'operation:'+row['space_id']+':'+ident,saved);self.restore_maps(row,saved)
                if original['deleted']:self.copy_deletion(uid,old_space,op_hash,row['space_id'],ident)
                with self.store.tx(immediate=True) as c:self.save_op(c,uid,ident,value,cipher,state)
                converted[op_hash]=ident;return ident
            for op_hash in by_hash:convert(op_hash)
