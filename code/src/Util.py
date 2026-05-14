import importlib
import re
import os
from src.SysLogger import CSysLogger
logger = CSysLogger("Util")


def loadPyFile(fileName, className, packageName=None):
    """
    
    @desc: 支持自定义导入文件[模型文件名]
    @params: fileName
    @params: className
    @params: packageName
    @reuturns: classTemplate
    """
    try:
        if fileName.endswith('py'):
            fileName = fileName.split('.py')[0]
        print(fileName)
        moudleFile = importlib.import_module('.'+fileName, packageName)
        classTemplate = getattr(moudleFile, className)
        
        return classTemplate
    except Exception as e:
        logger.error("Util.LoadPyFile, load file: {} - [{}]".format(fileName, e))

        return None

"""
获得某个文件夹下的group应该是group-xxx的标识
"""
def get_next_group_id(group_id):
    pattern = re.compile(r"^group-(\d+)$")
    match = pattern.fullmatch(group_id)

    num_str = match.group(1)
    next_num = int(num_str) + 1
    width = len(num_str)
    return f"group-{next_num:0{width}d}"

"""
获得某个文件夹下的最大的group-xxx的标识
"""
def get_max_group_id(group_list):
    max_num = 0
    
    for name in group_list:
        name = os.path.basename(name.strip("/"))
        pattern = re.compile(r"group-(\d+)")
        match = pattern.fullmatch(name)
        if match:
            num = int(match.group(1))
            if num > max_num:
                max_num = num

    # 自动适配前导0的宽度，比如 group-00001、group-00123
    width = len(match.group(1)) if match else 5
    return f"group-{max_num:0{width}d}"



def xyxy_to_xywh(xyxy):
    x1, y1, x2, y2 = xyxy
    x = round(float(x1), 2)
    y = round(float(y1), 2)
    w = round(float(x2 - x1), 2)
    h = round(float(y2 - y1), 2)
    return [x, y, w, h]

"""
自增group_id
group-001 -> group-002
group-012 -> group-013
group-123 -> group-124
....
"""
def auto_incre_group(group_id):
    prefix, number = group_id.split('-')
    new_number = str(int(number) + 1).zfill(len(number))
    return f"{prefix}-{new_number}"



if __name__ == '__main__':
    aa = ['AIDataManage/ImgObjRecognition/keep', 'AIDataManage/ImgObjRecognition/group-01122/', 'AIDataManage/ImgObjRecognition/model_list/']
    data = get_max_group_id(aa)
    print(data)
    data2 = get_next_group_id(data)
    print(data2)