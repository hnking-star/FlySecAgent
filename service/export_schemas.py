"""Generate Pi tool schemas from the Python protocol; no second handwritten schema."""
import json
import copy
from pathlib import Path
from .schemas import MemoryReadInput, MemoryCommitInput


def generate():
    def portable(model):
        schema = model.model_json_schema()
        definitions = schema.get('$defs', {})
        def expand(value, stack=()):
            if isinstance(value, list): return [expand(item, stack) for item in value]
            if not isinstance(value, dict): return value
            if '$ref' in value:
                key = value['$ref'].removeprefix('#/$defs/')
                if key not in definitions or key in stack: raise ValueError('Unsupported recursive schema reference')
                return expand({**copy.deepcopy(definitions[key]), **{k:v for k,v in value.items() if k!='$ref'}}, (*stack,key))
            return {key:expand(item,stack) for key,item in value.items() if key!='$defs'}
        return expand(schema)
    return {"protocol": 2, "curator_read": portable(MemoryReadInput), "curator_commit": portable(MemoryCommitInput)}


if __name__ == '__main__':
    path = Path(__file__).resolve().parent.parent / 'pi_ext/resources/curator-tools.schema.json'
    path.write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + '\n')
    print(path)
