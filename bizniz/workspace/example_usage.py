from bizniz.workspace.local_workspace import LocalWorkspace
from bizniz.workspace.temp_workspace import TempWorkspace




if __name__ == "__main__":
    
    # Example usage of the LocalWorkspace //////////////////////////////////////
    local_workspace = LocalWorkspace(root="./my_local_workspace")
    local_workspace.write_file("example.txt", "Hello, Local Workspace!")
    print("Local Workspace File Content:", local_workspace.read_file("example.txt"))
    for file in local_workspace.list_files():
        print("Local Workspace File:", file)
        
    
        
    
    print(local_workspace.tree())
    local_workspace.delete_file("example.txt")
    
    
    # Example usage of the TempWorkspace ////////////////////////////////////////
    with TempWorkspace() as temp_workspace:
        temp_workspace.write_file("temp_example.txt", "Hello, Temp Workspace!")
        print("Temp Workspace File Content:", temp_workspace.read_file("temp_example.txt"))
        temp_workspace.list_files()
        
        for file in temp_workspace.list_files():
            print("Temp Workspace File:", file)
            
        print(temp_workspace.tree())
        temp_workspace.delete_file("temp_example.txt")